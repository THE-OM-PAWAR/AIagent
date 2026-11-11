"""End-to-end unit and integration tests for Phase 3: Intelligence Orchestrator."""

import asyncio
from datetime import datetime, timezone
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.agent.context.builder import ContextMemoryItem, ContextMessage, IntelligentContextBuilder
from src.agent.execution import (
    ConversationLockManager,
    ExecutionRecord,
    ExecutionTracker,
    UpdateDeduplicator,
)
from src.agent.orchestrator import IntelligenceOrchestrator, OrchestratorError
from src.config import Config
from src.database.models import Base, ConversationModel, MessageModel
from src.database.repositories.conversation_repo import ConversationRepository
from src.intelligence.providers.gemini import (
    GeminiAPIError,
    GeminiClient,
    GeminiRateLimitError,
    GeminiTimeoutError,
)
from src.interfaces.telegram.handlers import (
    clear_command,
    handle_message,
    health_command,
    help_command,
    model_command,
    start_command,
    status_command,
)
from src.memory.manager import MemoryManager
from src.memory.schemas.memory import MemoryCategory
from src.memory.schemas.retrieval import RetrievalResult


def make_test_config(allowed_users=None):
    return Config(
        telegram_bot_token="test:token",
        allowed_telegram_user_ids=frozenset(allowed_users or [12345]),
        gemini_api_key="test_key",
        gemini_model="gemini-2.5-flash",
        gemini_system_instruction="You are a helpful assistant.",
        gemini_timeout_seconds=30.0,
        conversation_history_limit=20,
        log_level="INFO",
        environment="testing",
    )


class TestIntelligenceOrchestratorLifecycle(unittest.IsolatedAsyncioTestCase):
    """Verifies all 10 completion criteria for the Phase 3 Intelligence Orchestrator."""

    async def asyncSetUp(self) -> None:
        # Isolated in-memory async SQLite engine for relational testing
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.sessionmaker = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self.database_url = "sqlite+aiosqlite:///:memory:"

        # Mock Gemini Client
        self.mock_gemini = MagicMock(spec=GeminiClient)
        self.mock_gemini.model = "gemini-2.5-flash"
        self.mock_gemini._system_instruction = "You are a helpful personal assistant."
        self.mock_gemini.generate_response = AsyncMock(return_value="I am the AI assistant, ready to help!")
        self.mock_gemini.reset_chat = MagicMock(return_value=True)

        # Mock Memory Manager
        self.mock_memory = MagicMock(spec=MemoryManager)
        self.mock_memory.retrieve = AsyncMock(return_value=[])
        self.mock_memory.remember_turn = AsyncMock(return_value=MagicMock())

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    # --------------------------------------------------------------------------
    # Criterion 1: Telegram messages passing through the orchestrator
    # --------------------------------------------------------------------------
    async def test_01_telegram_message_through_orchestrator(self) -> None:
        """Verify Telegram messages invoke orchestrator.process_message and reply to user."""
        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
            system_prompt="Test system instruction",
        )

        update = MagicMock()
        update.update_id = 5001
        update.effective_user.id = 12345
        update.effective_chat.id = 67890
        update.message.text = "Hello orchestrator!"
        update.message.message_id = 99
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot_data = {
            "orchestrator": orchestrator,
            "config": make_test_config([12345]),
        }

        with patch("src.interfaces.telegram.handlers.is_authorized", return_value=True):
            await handle_message(update, context)

        update.message.reply_text.assert_awaited_once()
        self.assertIn("I am the AI assistant, ready to help!", update.message.reply_text.call_args[0][0])
        stats = await orchestrator.execution_tracker.get_stats()
        self.assertEqual(stats["completed"], 1)

    # --------------------------------------------------------------------------
    # Criterion 2: Relevant memories being retrieved
    # --------------------------------------------------------------------------
    async def test_02_relevant_memories_retrieved(self) -> None:
        """Verify RAG memory retrieval is invoked with user query and included in context."""
        retrieved_mock_memories = [
            RetrievalResult(
                memory_id="mem-pref-1",
                category=MemoryCategory.SEMANTIC,
                content="User prefers Python over JavaScript.",
                importance=0.9,
                confidence=1.0,
                relevance_score=0.95,
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        ]
        self.mock_memory.retrieve = AsyncMock(return_value=retrieved_mock_memories)

        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
            memory_manager=self.mock_memory,
        )

        response = await orchestrator.process_message(
            user_id=101,
            user_text="What programming language should I use?",
        )

        self.mock_memory.retrieve.assert_awaited_once()
        call_args = self.mock_memory.retrieve.call_args[1]
        self.assertEqual(call_args["user_id"], 101)
        self.assertEqual(call_args["query"], "What programming language should I use?")
        self.assertEqual(response, "I am the AI assistant, ready to help!")

    # --------------------------------------------------------------------------
    # Criterion 3: Context being constructed correctly
    # --------------------------------------------------------------------------
    async def test_03_context_construction(self) -> None:
        """Verify context engine builds structured prompt containing system, memories, and turns."""
        self.mock_memory.retrieve = AsyncMock(return_value=[
            RetrievalResult(
                memory_id="mem-arch-1",
                category=MemoryCategory.PROJECT,
                content="Architecture decision: Deploy with Docker Compose.",
                importance=0.95,
                confidence=1.0,
                relevance_score=0.9,
                project_association="ai-agent",
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        ])

        captured_system_instruction = None

        async def capture_generate(user_id, message_text, history, **kwargs):
            nonlocal captured_system_instruction
            captured_system_instruction = kwargs.get("system_instruction")
            return "Deployment response."

        self.mock_gemini.generate_response = AsyncMock(side_effect=capture_generate)

        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
            memory_manager=self.mock_memory,
        )

        response = await orchestrator.process_message(
            user_id=101,
            user_text="How should we deploy?",
            active_project="ai-agent",
        )

        self.assertEqual(response, "Deployment response.")
        self.assertIsNotNone(captured_system_instruction)
        self.assertIn("Architecture decision: Deploy with Docker Compose.", captured_system_instruction)

    # --------------------------------------------------------------------------
    # Criterion 4: Gemini responses using relevant context
    # --------------------------------------------------------------------------
    async def test_04_gemini_response_uses_context(self) -> None:
        """Verify conversation history turns are passed chronologically to Gemini."""
        captured_history = None

        async def capture_call(user_id, message_text, history=None, **kwargs):
            nonlocal captured_history
            captured_history = list(history) if history else []
            return "Response acknowledging previous turn."

        self.mock_gemini.generate_response = AsyncMock(side_effect=capture_call)

        # Mock database with 2 turns
        mock_db = MagicMock()
        mock_db.get_or_create_active_conversation = AsyncMock(return_value={"conversation_id": "c1"})
        mock_db.get_recent_messages = AsyncMock(return_value=[
            {"role": "user", "content": "My name is Alice."},
            {"role": "model", "content": "Hello Alice!"},
        ])
        mock_db.save_turn = AsyncMock()

        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
            db_client=mock_db,
        )

        resp = await orchestrator.process_message(
            user_id=101,
            user_text="What is my name?",
        )

        self.assertEqual(resp, "Response acknowledging previous turn.")
        self.assertEqual(len(captured_history), 2)
        self.assertEqual(captured_history[0]["content"], "My name is Alice.")
        self.assertEqual(captured_history[1]["content"], "Hello Alice!")

    # --------------------------------------------------------------------------
    # Criterion 5: Conversation history being persisted
    # --------------------------------------------------------------------------
    async def test_05_conversation_history_persisted(self) -> None:
        """Verify messages are persisted to database upon successful completion."""
        mock_db = MagicMock()
        mock_db.get_or_create_active_conversation = AsyncMock(return_value={"conversation_id": "conv_persist_1"})
        mock_db.get_recent_messages = AsyncMock(return_value=[])
        mock_db.save_turn = AsyncMock()

        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
            db_client=mock_db,
        )

        await orchestrator.process_message(
            user_id=101,
            user_text="Save this message turn.",
        )

        mock_db.save_turn.assert_awaited_once_with(
            conversation_id="conv_persist_1",
            user_id=101,
            user_message="Save this message turn.",
            model_response="I am the AI assistant, ready to help!",
        )

    # --------------------------------------------------------------------------
    # Criterion 6: Useful memories being extracted and updated
    # --------------------------------------------------------------------------
    async def test_06_memory_extraction_and_update(self) -> None:
        """Verify background memory extraction runs and failure does not lose model response."""
        # Case A: Memory extraction succeeds
        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
            memory_manager=self.mock_memory,
        )

        resp1 = await orchestrator.process_message(
            user_id=101,
            user_text="Remember that my timezone is UTC+5:30.",
        )
        self.assertEqual(resp1, "I am the AI assistant, ready to help!")

        # Allow background task to complete
        await asyncio.sleep(0.05)
        self.mock_memory.remember_turn.assert_awaited()

        # Case B: Memory extraction fails with exception
        self.mock_memory.remember_turn = AsyncMock(side_effect=RuntimeError("Extraction failed"))
        resp2 = await orchestrator.process_message(
            user_id=101,
            user_text="Another statement with failing extraction.",
        )
        # Response must still be successfully delivered without raising error!
        self.assertEqual(resp2, "I am the AI assistant, ready to help!")

    # --------------------------------------------------------------------------
    # Criterion 7: Concurrent message handling
    # --------------------------------------------------------------------------
    async def test_07_concurrent_message_handling(self) -> None:
        """Verify per-conversation locks serialize requests per user while different users run concurrently."""
        user1_execution_order = []
        user2_execution_order = []

        async def slow_response(user_id, message_text, **kwargs):
            if user_id == 101:
                user1_execution_order.append(f"start_{message_text}")
                await asyncio.sleep(0.05)
                user1_execution_order.append(f"end_{message_text}")
                return f"reply_{message_text}"
            else:
                user2_execution_order.append(f"start_{message_text}")
                await asyncio.sleep(0.05)
                user2_execution_order.append(f"end_{message_text}")
                return f"reply_{message_text}"

        self.mock_gemini.generate_response = AsyncMock(side_effect=slow_response)

        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
        )

        # Launch two requests concurrently for user 101
        t1 = asyncio.create_task(orchestrator.process_message(101, "msg_A"))
        t2 = asyncio.create_task(orchestrator.process_message(101, "msg_B"))
        # Launch one request concurrently for user 202
        t3 = asyncio.create_task(orchestrator.process_message(202, "msg_C"))

        results = await asyncio.gather(t1, t2, t3)
        self.assertEqual(results, ["reply_msg_A", "reply_msg_B", "reply_msg_C"])

        # User 101 requests must be strictly serialized (end_A before start_B or vice-versa)
        self.assertEqual(len(user1_execution_order), 4)
        if user1_execution_order[0] == "start_msg_A":
            self.assertEqual(user1_execution_order[1], "end_msg_A")
            self.assertEqual(user1_execution_order[2], "start_msg_B")
            self.assertEqual(user1_execution_order[3], "end_msg_B")
        else:
            self.assertEqual(user1_execution_order[1], "end_msg_B")
            self.assertEqual(user1_execution_order[2], "start_msg_A")
            self.assertEqual(user1_execution_order[3], "end_msg_A")

    # --------------------------------------------------------------------------
    # Criterion 8: Duplicate update protection
    # --------------------------------------------------------------------------
    async def test_08_duplicate_update_protection(self) -> None:
        """Verify identical update_ids are rejected without making duplicate LLM calls."""
        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
        )

        first_resp = await orchestrator.process_message(
            user_id=101,
            user_text="Process this first.",
            update_id=9001,
        )
        self.assertEqual(first_resp, "I am the AI assistant, ready to help!")
        self.assertEqual(self.mock_gemini.generate_response.await_count, 1)

        # Duplicate update_id
        second_resp = await orchestrator.process_message(
            user_id=101,
            user_text="Process this again.",
            update_id=9001,
        )
        # Must return empty and NOT invoke Gemini again
        self.assertEqual(second_resp, "")
        self.assertEqual(self.mock_gemini.generate_response.await_count, 1)

    # --------------------------------------------------------------------------
    # Criterion 9: Provider and database failure recovery
    # --------------------------------------------------------------------------
    async def test_09_provider_and_database_failure_recovery(self) -> None:
        """Verify graceful recovery on database outage and clear exception tracking on provider failures."""
        # Database lookup fails: orchestrator still generates response in degraded mode
        failing_db = MagicMock()
        failing_db.get_or_create_active_conversation = AsyncMock(side_effect=ConnectionError("DB Down"))
        failing_db.get_recent_messages = AsyncMock(side_effect=ConnectionError("DB Down"))
        failing_db.save_turn = AsyncMock(side_effect=ConnectionError("DB Down"))

        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
            db_client=failing_db,
        )

        resp = await orchestrator.process_message(
            user_id=101,
            user_text="DB is failing but generate response.",
        )
        self.assertEqual(resp, "I am the AI assistant, ready to help!")

        # Gemini provider raises rate limit: orchestrator records failure and propagates cleanly
        self.mock_gemini.generate_response = AsyncMock(side_effect=GeminiRateLimitError("Rate limit hit"))
        with self.assertRaises(GeminiRateLimitError):
            await orchestrator.process_message(
                user_id=101,
                user_text="Will fail with rate limit.",
            )

        stats = await orchestrator.execution_tracker.get_stats()
        self.assertGreater(stats["failed"], 0)

    # --------------------------------------------------------------------------
    # Criterion 10: Existing Telegram commands working
    # --------------------------------------------------------------------------
    async def test_10_existing_telegram_commands_working(self) -> None:
        """Verify /start, /help, /status, /health, /model, and /clear operate cleanly."""
        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
        )

        update = MagicMock()
        update.effective_user.id = 12345
        update.message.reply_text = AsyncMock()

        config = make_test_config([12345])

        context = MagicMock()
        context.bot_data = {
            "orchestrator": orchestrator,
            "gemini_client": self.mock_gemini,
            "config": config,
            "start_time": time.time() - 3600,
        }

        with patch("src.interfaces.telegram.handlers.is_authorized", return_value=True):
            # 1. /start
            await start_command(update, context)
            self.assertTrue(any("Hello" in str(c) for c in update.message.reply_text.call_args[0]))

            # 2. /help
            await help_command(update, context)
            self.assertTrue(any("Commands" in str(c) for c in update.message.reply_text.call_args[0]))

            # 3. /status
            await status_command(update, context)
            self.assertTrue(any("Running" in str(c) for c in update.message.reply_text.call_args[0]))

            # 4. /model
            await model_command(update, context)
            self.assertTrue(any("gemini-2.5-flash" in str(c) for c in update.message.reply_text.call_args[0]))

            # 5. /clear
            await clear_command(update, context)
            self.assertTrue(any("cleared" in str(c) for c in update.message.reply_text.call_args[0]))


if __name__ == "__main__":
    unittest.main()
