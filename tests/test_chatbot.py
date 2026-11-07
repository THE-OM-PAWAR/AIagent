"""Comprehensive unit and integration tests for Telegram, Gemini, MongoDB, and Production Features."""

import asyncio
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Chat, Message, Update, User

from src.config import Config, ConfigError
from src.database import DatabaseClient, mask_mongo_uri
from src.gemini import (
    GeminiAPIError,
    GeminiClient,
    GeminiRateLimitError,
    GeminiTimeoutError,
)
from src.handlers.handlers import (
    DuplicateMessageFilter,
    clear_command,
    help_command,
    health_command,
    handle_message,
    is_authorized,
    send_typing_action,
    split_message,
    start_command,
)
from src.healthcheck import run_health_check


class TestConfig(unittest.TestCase):
    """Test environment configuration loading, validation, and authorization list parsing."""

    def test_valid_configuration(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            "ALLOWED_TELEGRAM_USER_IDS": "12345678, 87654321",
            "GEMINI_API_KEY": "test_gemini_key",
            "GEMINI_MODEL": "gemini-2.5-flash",
            "GEMINI_SYSTEM_INSTRUCTION": "You are a test assistant.",
            "GEMINI_TIMEOUT_SECONDS": "45.0",
            "MONGODB_URI": "mongodb://localhost:27017",
            "MONGODB_DB_NAME": "test_db",
            "CONVERSATION_HISTORY_LIMIT": "15",
            "LOG_LEVEL": "DEBUG",
            "ENVIRONMENT": "production",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.load()
            self.assertEqual(config.telegram_bot_token, "123456:ABC-DEF")
            self.assertEqual(config.allowed_telegram_user_ids, frozenset({12345678, 87654321}))
            self.assertEqual(config.gemini_api_key, "test_gemini_key")
            self.assertEqual(config.gemini_model, "gemini-2.5-flash")
            self.assertEqual(config.gemini_system_instruction, "You are a test assistant.")
            self.assertEqual(config.gemini_timeout_seconds, 45.0)
            self.assertEqual(config.conversation_history_limit, 15)
            self.assertEqual(config.log_level, "DEBUG")
            self.assertEqual(config.environment, "production")

    def test_missing_allowed_users_fails_validation(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            "ALLOWED_TELEGRAM_USER_IDS": "",
            "GEMINI_API_KEY": "test_gemini_key",
            "GEMINI_MODEL": "gemini-2.5-flash",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                Config.load()
            self.assertIn("ALLOWED_TELEGRAM_USER_IDS", str(ctx.exception))

    def test_invalid_allowed_users_fails_validation(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            "ALLOWED_TELEGRAM_USER_IDS": "12345, not_a_number, 6789",
            "GEMINI_API_KEY": "test_gemini_key",
            "GEMINI_MODEL": "gemini-2.5-flash",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                Config.load()
            self.assertIn("ALLOWED_TELEGRAM_USER_IDS", str(ctx.exception))

    def test_missing_model_fails_validation(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            "ALLOWED_TELEGRAM_USER_IDS": "12345",
            "GEMINI_API_KEY": "test_gemini_key",
            "GEMINI_MODEL": "",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                Config.load()
            self.assertIn("GEMINI_MODEL", str(ctx.exception))


class TestDuplicateMessageFilter(unittest.IsolatedAsyncioTestCase):
    """Test in-memory bounded update deduplication."""

    async def test_deduplication_and_capacity_eviction(self) -> None:
        filter_cache = DuplicateMessageFilter(capacity=3)

        # First time update_id 100 is seen -> not duplicate
        self.assertFalse(await filter_cache.is_duplicate(100))
        # Second time update_id 100 is seen -> duplicate!
        self.assertTrue(await filter_cache.is_duplicate(100))

        # Add more update_ids up to capacity
        self.assertFalse(await filter_cache.is_duplicate(101))
        self.assertFalse(await filter_cache.is_duplicate(102))

        # Adding 4th item evicts the oldest (100)
        self.assertFalse(await filter_cache.is_duplicate(103))
        # Now 100 has been evicted from bounded cache
        self.assertFalse(await filter_cache.is_duplicate(100))


class TestDatabaseSecurityAndLifecycle(unittest.IsolatedAsyncioTestCase):
    """Test URI masking, connection failure handling, and index creation."""

    def test_mask_mongo_uri(self) -> None:
        raw_uri = "mongodb://bot_user:secret_pass123@cluster.mongodb.net:27017/db"
        masked = mask_mongo_uri(raw_uri)
        self.assertNotIn("secret_pass123", masked)
        self.assertNotIn("bot_user", masked)
        self.assertIn("mongodb://***:***@cluster.mongodb.net", masked)

    def test_mask_mongo_uri_no_credentials(self) -> None:
        raw_uri = "mongodb://localhost:27017"
        self.assertEqual(mask_mongo_uri(raw_uri), "mongodb://localhost:27017")

    async def test_connect_failure_raises(self) -> None:
        db = DatabaseClient("mongodb://127.0.0.1:54321/?serverSelectionTimeoutMS=500", "test_db")
        with self.assertRaises(Exception):
            await db.connect()


class TestDatabaseOperations(unittest.IsolatedAsyncioTestCase):
    """Test conversation metadata and message persistence with live or mocked MongoDB."""

    async def asyncSetUp(self) -> None:
        self.db_name = f"test_telegram_ai_{int(datetime.now(timezone.utc).timestamp())}"
        self.db = DatabaseClient("mongodb://localhost:27017", self.db_name)
        try:
            await self.db.connect()
            self.live_mongo = True
        except Exception:
            self.live_mongo = False

    async def asyncTearDown(self) -> None:
        if self.live_mongo:
            try:
                await self.db._client.drop_database(self.db_name)
            except Exception:
                pass
            await self.db.close()

    async def test_get_or_create_and_clear_conversation(self) -> None:
        if not self.live_mongo:
            self.skipTest("Local MongoDB not available")

        user_id = 12345
        conv1 = await self.db.get_or_create_active_conversation(user_id)
        self.assertTrue(conv1["is_active"])
        conv1_id = conv1["conversation_id"]

        conv1_fetch = await self.db.get_or_create_active_conversation(user_id)
        self.assertEqual(conv1_fetch["conversation_id"], conv1_id)

        cleared = await self.db.clear_active_conversation(user_id)
        self.assertTrue(cleared)

        conv2 = await self.db.get_or_create_active_conversation(user_id)
        self.assertTrue(conv2["is_active"])
        self.assertNotEqual(conv2["conversation_id"], conv1_id)

    async def test_save_turn_and_chronological_retrieval(self) -> None:
        if not self.live_mongo:
            self.skipTest("Local MongoDB not available")

        user_id = 88888
        conv = await self.db.get_or_create_active_conversation(user_id)
        conv_id = conv["conversation_id"]

        await self.db.save_turn(conv_id, user_id, "User Q1", "Model A1")
        await self.db.save_turn(conv_id, user_id, "User Q2", "Model A2")

        updated_conv = await self.db.conversations.find_one({"conversation_id": conv_id})
        self.assertEqual(updated_conv["message_count"], 4)

        messages = await self.db.get_recent_messages(conv_id, limit=10)
        self.assertEqual(len(messages), 4)

        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "User Q1")
        self.assertEqual(messages[1]["role"], "model")
        self.assertEqual(messages[1]["content"], "Model A1")
        self.assertEqual(messages[2]["role"], "user")
        self.assertEqual(messages[2]["content"], "User Q2")
        self.assertEqual(messages[3]["role"], "model")
        self.assertEqual(messages[3]["content"], "Model A2")

    async def test_context_limit_truncation(self) -> None:
        if not self.live_mongo:
            self.skipTest("Local MongoDB not available")

        user_id = 77777
        conv = await self.db.get_or_create_active_conversation(user_id)
        conv_id = conv["conversation_id"]

        for i in range(1, 7):
            await self.db.save_turn(conv_id, user_id, f"Question {i}", f"Answer {i}")

        messages = await self.db.get_recent_messages(conv_id, limit=4)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0]["content"], "Question 5")
        self.assertEqual(messages[1]["content"], "Answer 5")
        self.assertEqual(messages[2]["content"], "Question 6")
        self.assertEqual(messages[3]["content"], "Answer 6")

    async def test_persistence_across_restarts_and_clear(self) -> None:
        if not self.live_mongo:
            self.skipTest("Local MongoDB not available")

        user_id = 991199

        # --- SESSION 1 ---
        conv1 = await self.db.get_or_create_active_conversation(user_id)
        conv1_id = conv1["conversation_id"]

        await self.db.save_turn(
            conv1_id,
            user_id,
            "Hello! My favorite programming language is Python.",
            "Great choice! Python is powerful and readable.",
        )

        await self.db.close()

        # --- SESSION 2 ---
        db_restarted = DatabaseClient("mongodb://localhost:27017", self.db_name)
        await db_restarted.connect()
        try:
            conv_resumed = await db_restarted.get_or_create_active_conversation(user_id)
            self.assertEqual(conv_resumed["conversation_id"], conv1_id)

            history = await db_restarted.get_recent_messages(conv_resumed["conversation_id"], limit=20)
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["content"], "Hello! My favorite programming language is Python.")
            self.assertEqual(history[1]["content"], "Great choice! Python is powerful and readable.")

            await db_restarted.clear_active_conversation(user_id)

            conv_new = await db_restarted.get_or_create_active_conversation(user_id)
            self.assertNotEqual(conv_new["conversation_id"], conv1_id)
            new_history = await db_restarted.get_recent_messages(conv_new["conversation_id"], limit=20)
            self.assertEqual(len(new_history), 0)
        finally:
            await db_restarted.close()
            await self.db.connect()


class TestGeminiClient(unittest.IsolatedAsyncioTestCase):
    """Test Gemini client conversational logic with history and error handling."""

    def setUp(self) -> None:
        self.gemini = GeminiClient(
            api_key="mock_api_key",
            model="gemini-2.5-flash",
            system_instruction="Be helpful.",
            timeout=10.0,
        )
        self.gemini._client = MagicMock()

    async def test_generate_response_with_history(self) -> None:
        mock_chat = AsyncMock()
        mock_response = MagicMock(text="Paris is the capital of France.")
        mock_chat.send_message.return_value = mock_response

        mock_chats = MagicMock()
        mock_chats.create.return_value = mock_chat
        self.gemini.client.aio.chats = mock_chats

        history = [
            {"role": "user", "content": "What country is Paris in?"},
            {"role": "model", "content": "Paris is in France."},
        ]

        reply = await self.gemini.generate_response(
            user_id=101,
            message_text="What is its capital?",
            history=history,
        )

        self.assertEqual(reply, "Paris is the capital of France.")
        mock_chats.create.assert_called_once()
        created_kwargs = mock_chats.create.call_args[1]
        self.assertIsNotNone(created_kwargs.get("history"))
        self.assertEqual(len(created_kwargs["history"]), 2)

    async def test_timeout_handling(self) -> None:
        mock_chat = AsyncMock()
        async def slow_response(*args, **kwargs):
            await asyncio.sleep(2.0)
        mock_chat.send_message.side_effect = slow_response

        self.gemini._timeout = 0.05
        with patch.object(self.gemini, "get_or_create_chat", return_value=mock_chat):
            with self.assertRaises(GeminiTimeoutError):
                await self.gemini.generate_response(101, "A slow question")

    async def test_rate_limit_handling(self) -> None:
        from google.genai import errors
        mock_chat = AsyncMock()
        mock_chat.send_message.side_effect = errors.APIError(
            code=429,
            response_json={"error": {"message": "Resource has been exhausted (quota)."}},
        )

        with patch.object(self.gemini, "get_or_create_chat", return_value=mock_chat):
            with self.assertRaises(GeminiRateLimitError):
                await self.gemini.generate_response(101, "A rate limited question")


class TestTelegramHandlersWithAuth(unittest.IsolatedAsyncioTestCase):
    """Test authorization, commands, deduplication, and message handling."""

    def setUp(self) -> None:
        self.bot = AsyncMock()
        self.authorized_user = User(id=999, first_name="Admin", is_bot=False)
        self.unauthorized_user = User(id=403, first_name="Attacker", is_bot=False)
        self.chat = Chat(id=12345, type="private")

        self.message = AsyncMock(spec=Message)
        self.message.message_id = 555
        self.message.text = "Hello Gemini"
        self.message.reply_text = AsyncMock()

        self.update = MagicMock(spec=Update)
        self.update.update_id = 1001
        self.update.effective_user = self.authorized_user
        self.update.effective_chat = self.chat
        self.update.message = self.message

        self.context = MagicMock()
        self.context.bot = self.bot
        self.gemini_mock = AsyncMock(spec=GeminiClient)
        self.db_mock = AsyncMock(spec=DatabaseClient)
        self.config_mock = MagicMock(
            allowed_telegram_user_ids=frozenset({999}),
            conversation_history_limit=10,
            gemini_model="gemini-2.5-flash",
            log_level="INFO",
            environment="production",
        )

        self.context.bot_data = {
            "config": self.config_mock,
            "gemini_client": self.gemini_mock,
            "db_client": self.db_mock,
        }

    async def test_unauthorized_user_is_blocked(self) -> None:
        self.update.effective_user = self.unauthorized_user

        await handle_message(self.update, self.context)

        # Gemini and DB must NOT be called
        self.gemini_mock.generate_response.assert_not_called()
        self.db_mock.get_or_create_active_conversation.assert_not_called()

        # Access denied message delivered
        self.message.reply_text.assert_awaited_once()
        reply_content = self.message.reply_text.call_args[0][0]
        self.assertIn("Access denied", reply_content)

    async def test_authorized_user_message_handling(self) -> None:
        self.db_mock.get_or_create_active_conversation.return_value = {
            "conversation_id": "conv_123",
            "user_id": 999,
        }
        self.db_mock.get_recent_messages.return_value = []
        self.gemini_mock.generate_response.return_value = "AI response text."

        await handle_message(self.update, self.context)

        self.gemini_mock.generate_response.assert_awaited_once_with(
            user_id=999,
            message_text="Hello Gemini",
            history=[],
        )
        self.db_mock.save_turn.assert_awaited_once_with(
            conversation_id="conv_123",
            user_id=999,
            user_message="Hello Gemini",
            model_response="AI response text.",
        )
        self.message.reply_text.assert_awaited_once_with("AI response text.")

    async def test_start_command_authorization(self) -> None:
        # Authorized
        await start_command(self.update, self.context)
        self.db_mock.clear_active_conversation.assert_awaited_once_with(999)
        self.message.reply_text.assert_awaited_once()

        # Unauthorized
        self.update.effective_user = self.unauthorized_user
        self.message.reply_text.reset_mock()
        await start_command(self.update, self.context)
        reply = self.message.reply_text.call_args[0][0]
        self.assertIn("Access denied", reply)

    async def test_health_command(self) -> None:
        self.db_mock.db.command = AsyncMock(return_value={"ok": 1})
        await health_command(self.update, self.context)
        self.message.reply_text.assert_awaited_once()
        reply = self.message.reply_text.call_args[0][0]
        self.assertIn("System Health Status", reply)
        self.assertIn("Operational", reply)


class TestHealthCheckCLI(unittest.IsolatedAsyncioTestCase):
    """Test CLI health check script behavior."""

    async def test_health_check_reporting(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            "ALLOWED_TELEGRAM_USER_IDS": "999",
            "GEMINI_API_KEY": "test_key",
            "GEMINI_MODEL": "gemini-2.5-flash",
            "MONGODB_URI": "mongodb://localhost:27017",
            "MONGODB_DB_NAME": "test_db",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("src.healthcheck.Bot.get_me", new=AsyncMock(return_value=MagicMock(username="test_bot"))):
                is_healthy, report = await run_health_check()
                self.assertIn("status", report)
                self.assertIn("checks", report)
                self.assertIn("config", report["checks"])
                self.assertEqual(report["checks"]["config"]["status"], "ok")


class TestMessageUtilities(unittest.TestCase):
    """Test text splitting logic and character limit handling."""

    def test_split_message_within_limit(self) -> None:
        text = "Short text"
        chunks = split_message(text, max_length=100)
        self.assertEqual(chunks, ["Short text"])

    def test_split_message_exceeding_limit(self) -> None:
        paragraphs = ["Paragraph 1 " * 50, "Paragraph 2 " * 50, "Paragraph 3 " * 50]
        long_text = "\n\n".join(paragraphs)
        chunks = split_message(long_text, max_length=200)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 200)

        rejoined = " ".join(chunks)
        self.assertIn("Paragraph 1", rejoined)
        self.assertIn("Paragraph 2", rejoined)
        self.assertIn("Paragraph 3", rejoined)


if __name__ == "__main__":
    unittest.main()
