"""Unit and integration tests for Telegram Bot Commands Menu (setMyCommands), scopes, and handlers."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import unittest

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Update, User, Chat, Message
from telegram.error import TelegramError

from src.interfaces.telegram.commands import (
    CommandDefinition,
    DEFAULT_USER_COMMANDS,
    ADMIN_ONLY_COMMANDS,
    get_admin_commands,
    get_admin_user_ids,
    get_default_user_commands,
    format_help_text,
    setup_bot_commands,
    validate_command_definition,
    validate_commands_list,
    verify_bot_commands,
)
from src.interfaces.telegram.handlers import (
    help_command,
    memory_command,
    tools_command,
    workflows_command,
)


class TestTelegramCommandValidation(unittest.TestCase):
    """Test Telegram Bot API command naming, description, and list limits validation."""

    def test_valid_command_definition(self) -> None:
        cmd = CommandDefinition(command="test_cmd", description="A valid test command")
        validate_command_definition(cmd)
        bot_cmd = cmd.to_bot_command()
        self.assertIsInstance(bot_cmd, BotCommand)
        self.assertEqual(bot_cmd.command, "test_cmd")
        self.assertEqual(bot_cmd.description, "A valid test command")

    def test_empty_command_name_raises(self) -> None:
        cmd = CommandDefinition(command="", description="Missing name")
        with self.assertRaises(ValueError) as ctx:
            validate_command_definition(cmd)
        self.assertIn("cannot be empty", str(ctx.exception))

    def test_invalid_command_characters_raises(self) -> None:
        invalid_names = ["TestCmd", "test-cmd", "test cmd", "test!", "cmd/sub", "a" * 33]
        for name in invalid_names:
            with self.subTest(name=name):
                cmd = CommandDefinition(command=name, description="Sample desc")
                with self.assertRaises(ValueError) as ctx:
                    validate_command_definition(cmd)
                self.assertIn("Invalid command name", str(ctx.exception))

    def test_empty_or_too_long_description_raises(self) -> None:
        empty_desc = CommandDefinition(command="valid", description="")
        with self.assertRaises(ValueError):
            validate_command_definition(empty_desc)

        too_long = CommandDefinition(command="valid", description="x" * 257)
        with self.assertRaises(ValueError) as ctx:
            validate_command_definition(too_long)
        self.assertIn("exceeds maximum 256", str(ctx.exception))

    def test_validate_commands_list_detects_duplicates(self) -> None:
        cmds = [
            CommandDefinition(command="start", description="Start"),
            CommandDefinition(command="help", description="Help"),
            CommandDefinition(command="start", description="Duplicate start"),
        ]
        with self.assertRaises(ValueError) as ctx:
            validate_commands_list(cmds)
        self.assertIn("Duplicate command name", str(ctx.exception))

    def test_validate_commands_list_exceeds_max_limit(self) -> None:
        cmds = [
            CommandDefinition(command=f"cmd_{i}", description=f"Desc {i}")
            for i in range(101)
        ]
        with self.assertRaises(ValueError) as ctx:
            validate_commands_list(cmds)
        self.assertIn("exceeds Telegram maximum of 100", str(ctx.exception))


class TestTelegramCommandDefinitions(unittest.TestCase):
    """Test predefined command sets, scopes, and help formatting."""

    def test_default_commands_validity(self) -> None:
        user_cmds = get_default_user_commands()
        validated = validate_commands_list(user_cmds)
        self.assertGreaterEqual(len(validated), 10)
        command_names = [cmd.command for cmd in user_cmds]
        expected_subset = {
            "start", "help", "new", "clear", "memory", "tools",
            "workflows", "integrations", "connect", "disconnect", "cancel"
        }
        self.assertTrue(expected_subset.issubset(set(command_names)))

    def test_admin_commands_include_diagnostics(self) -> None:
        admin_cmds = get_admin_commands()
        validated = validate_commands_list(admin_cmds)
        self.assertGreater(len(validated), len(get_default_user_commands()))
        admin_names = {cmd.command for cmd in admin_cmds}
        self.assertIn("health", admin_names)
        self.assertIn("integration_status", admin_names)
        self.assertIn("status", admin_names)
        self.assertIn("model", admin_names)

    def test_get_admin_user_ids_resolution(self) -> None:
        # 1. Direct admin list provided
        cfg1 = MagicMock(
            admin_telegram_user_ids=frozenset({1001, 1002}),
            allowed_telegram_user_ids=frozenset({1001, 1002, 1003}),
        )
        self.assertEqual(get_admin_user_ids(cfg1), frozenset({1001, 1002}))

        # 2. Fallback to allowed users
        cfg2 = MagicMock(
            admin_telegram_user_ids=frozenset(),
            allowed_telegram_user_ids=frozenset({2001}),
        )
        self.assertEqual(get_admin_user_ids(cfg2), frozenset({2001}))

        # 3. None config
        self.assertEqual(get_admin_user_ids(None), frozenset())

    def test_format_help_text(self) -> None:
        user_help = format_help_text(is_admin=False)
        self.assertIn("Personal AI Assistant Commands", user_help)
        self.assertIn("/start", user_help)
        self.assertIn("/help", user_help)
        self.assertNotIn("Admin & Diagnostics", user_help)

        admin_help = format_help_text(is_admin=True)
        self.assertIn("Admin & Diagnostics", admin_help)
        self.assertIn("/health", admin_help)
        self.assertIn("/status", admin_help)


class TestTelegramCommandRegistration(unittest.IsolatedAsyncioTestCase):
    """Test setup_bot_commands execution, scoping, idempotency, and error tolerance."""

    async def test_setup_bot_commands_success(self) -> None:
        mock_bot = AsyncMock()
        mock_app = MagicMock()
        mock_app.bot = mock_bot
        mock_app.bot_data = {
            "config": MagicMock(admin_telegram_user_ids=frozenset({777, 888})),
        }

        success = await setup_bot_commands(mock_app)
        self.assertTrue(success)
        self.assertTrue(mock_app.bot_data.get("_commands_configured"))

        # Verify default scope call
        default_calls = [
            call for call in mock_bot.set_my_commands.call_args_list
            if isinstance(call.kwargs.get("scope"), BotCommandScopeDefault)
        ]
        self.assertEqual(len(default_calls), 1)

        # Verify chat-specific admin scope calls (one per admin id)
        admin_calls = [
            call for call in mock_bot.set_my_commands.call_args_list
            if isinstance(call.kwargs.get("scope"), BotCommandScopeChat)
        ]
        self.assertEqual(len(admin_calls), 2)
        admin_chat_ids = {call.kwargs["scope"].chat_id for call in admin_calls}
        self.assertEqual(admin_chat_ids, {777, 888})

    async def test_setup_bot_commands_idempotency(self) -> None:
        mock_bot = AsyncMock()
        mock_app = MagicMock()
        mock_app.bot = mock_bot
        mock_app.bot_data = {
            "_commands_configured": True,
            "config": MagicMock(admin_telegram_user_ids=frozenset({777})),
        }

        success = await setup_bot_commands(mock_app, force=False)
        self.assertTrue(success)
        mock_bot.set_my_commands.assert_not_called()

        # With force=True, it should execute
        success_forced = await setup_bot_commands(mock_app, force=True)
        self.assertTrue(success_forced)
        self.assertTrue(mock_bot.set_my_commands.called)

    async def test_setup_bot_commands_handles_telegram_error_gracefully(self) -> None:
        mock_bot = AsyncMock()
        mock_bot.set_my_commands.side_effect = TelegramError("Network connection reset")
        mock_app = MagicMock()
        mock_app.bot = mock_bot
        mock_app.bot_data = {"config": MagicMock(admin_telegram_user_ids=frozenset())}

        # Must not raise exception, but return False and log warning
        success = await setup_bot_commands(mock_app)
        self.assertFalse(success)

    async def test_verify_bot_commands(self) -> None:
        mock_bot = AsyncMock()
        mock_bot.get_my_commands.return_value = [
            BotCommand("start", "Start AIagent"),
            BotCommand("help", "View available commands"),
        ]
        res = await verify_bot_commands(mock_bot)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0].command, "start")


class TestTelegramCommandHandlers(unittest.IsolatedAsyncioTestCase):
    """Test handlers for /tools, /memory, and /workflows commands."""

    def setUp(self) -> None:
        self.user = User(id=123, is_bot=False, first_name="TestUser")
        self.chat = Chat(id=123, type="private")
        self.message = AsyncMock(spec=Message)
        self.update = MagicMock(spec=Update)
        self.update.effective_user = self.user
        self.update.effective_chat = self.chat
        self.update.message = self.message

        self.context = MagicMock()
        self.config = MagicMock(
            allowed_telegram_user_ids=frozenset({123}),
            admin_telegram_user_ids=frozenset({123}),
        )
        self.context.bot_data = {
            "config": self.config,
            "orchestrator": MagicMock(),
        }

    async def test_tools_command(self) -> None:
        self.context.bot_data["orchestrator"].tool_registry.list_tools.return_value = [
            {"name": "calculator", "description": "Perform math", "permission": "read_only"},
            {"name": "web_search", "description": "Search internet", "permission": "read_only"},
        ]

        await tools_command(self.update, self.context)
        self.message.reply_text.assert_awaited_once()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Available Agent Tools", text)
        self.assertIn("`calculator`", text)
        self.assertIn("`web_search`", text)

    @patch("src.database.session.get_async_session")
    @patch("src.database.repositories.memory_repo.MemoryRepository")
    async def test_memory_command_list_empty(self, mock_repo_cls: MagicMock, mock_get_session: MagicMock) -> None:
        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_session
        mock_get_session.return_value = mock_ctx

        mock_repo = AsyncMock()
        mock_repo.list_active.return_value = []
        mock_repo_cls.return_value = mock_repo

        self.config.database_url = "postgresql+asyncpg://user:pass@localhost:5432/test"
        self.context.args = []

        await memory_command(self.update, self.context)
        self.message.reply_text.assert_awaited_once()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("No active memories saved yet", text)

    @patch("src.database.session.get_async_session")
    @patch("src.database.repositories.memory_repo.MemoryRepository")
    async def test_memory_command_clear(self, mock_repo_cls: MagicMock, mock_get_session: MagicMock) -> None:
        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_session
        mock_get_session.return_value = mock_ctx

        mock_mem1 = MagicMock(id="mem1")
        mock_mem2 = MagicMock(id="mem2")
        mock_repo = AsyncMock()
        mock_repo.list_active.return_value = [mock_mem1, mock_mem2]
        mock_repo_cls.return_value = mock_repo

        self.config.database_url = "postgresql+asyncpg://user:pass@localhost:5432/test"
        self.context.args = ["clear"]

        await memory_command(self.update, self.context)
        self.message.reply_text.assert_awaited_once()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Successfully cleared 2 saved memories", text)

    async def test_workflows_command(self) -> None:
        mock_wf_engine = AsyncMock()
        mock_wf_engine.list_workflows.return_value = [
            {
                "workflow_id": "wf_abc_123",
                "name": "Data Sync",
                "status": "RUNNING",
                "current_step": 2,
            }
        ]
        self.context.bot_data["workflow_engine"] = mock_wf_engine

        await workflows_command(self.update, self.context)
        self.message.reply_text.assert_awaited_once()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Active Workflows", text)
        self.assertIn("wf_abc_123", text)


if __name__ == "__main__":
    unittest.main()
