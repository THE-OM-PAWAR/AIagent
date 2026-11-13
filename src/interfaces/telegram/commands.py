"""Centralized Telegram Bot Command definitions, validation, and menu registration."""

import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.ext import Application

logger = logging.getLogger(__name__)

# Telegram Command validation constraints:
# Command: 1-32 characters, lowercase English letters, digits, and underscores.
COMMAND_NAME_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")
MAX_DESCRIPTION_LENGTH = 256
MAX_COMMANDS_PER_SCOPE = 100


@dataclass(frozen=True)
class CommandDefinition:
    """Represents a validated bot command definition."""

    command: str
    description: str
    is_admin_only: bool = False
    category: str = "general"

    def to_bot_command(self) -> BotCommand:
        """Convert to python-telegram-bot BotCommand instance."""
        return BotCommand(command=self.command, description=self.description)


def validate_command_definition(cmd: CommandDefinition) -> None:
    """Validate a single command definition against Telegram Bot API constraints.

    Raises:
        ValueError: If the command name or description violates Telegram specifications.
    """
    if not cmd.command:
        raise ValueError("Command name cannot be empty.")
    if not COMMAND_NAME_PATTERN.match(cmd.command):
        raise ValueError(
            f"Invalid command name '{cmd.command}'. "
            "Must be 1-32 characters containing only lowercase English letters, digits, and underscores."
        )
    if not cmd.description:
        raise ValueError(f"Description for command '{cmd.command}' cannot be empty.")
    if len(cmd.description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"Description for command '{cmd.command}' exceeds maximum {MAX_DESCRIPTION_LENGTH} characters "
            f"({len(cmd.description)} characters given)."
        )


def validate_commands_list(commands: Sequence[CommandDefinition]) -> list[BotCommand]:
    """Validate a sequence of command definitions, ensuring uniqueness and limit adherence.

    Args:
        commands: Sequence of CommandDefinition objects.

    Returns:
        List of validated python-telegram-bot BotCommand objects.

    Raises:
        ValueError: If duplicate commands are detected or limit is exceeded.
    """
    if len(commands) > MAX_COMMANDS_PER_SCOPE:
        raise ValueError(
            f"Command list exceeds Telegram maximum of {MAX_COMMANDS_PER_SCOPE} commands "
            f"({len(commands)} commands given)."
        )

    seen_commands: set[str] = set()
    validated_bot_commands: list[BotCommand] = []

    for cmd in commands:
        validate_command_definition(cmd)
        normalized = cmd.command.lower().strip()
        if normalized in seen_commands:
            raise ValueError(f"Duplicate command name detected: '{cmd.command}'.")
        seen_commands.add(normalized)
        validated_bot_commands.append(cmd.to_bot_command())

    return validated_bot_commands


# -----------------------------------------------------------------------------
# Centralized Command Definitions
# -----------------------------------------------------------------------------

DEFAULT_USER_COMMANDS: list[CommandDefinition] = [
    CommandDefinition(command="start", description="Start AIagent", category="general"),
    CommandDefinition(command="help", description="View available commands", category="general"),
    CommandDefinition(command="new", description="Start a new conversation", category="conversation"),
    CommandDefinition(command="clear", description="Clear current conversation", category="conversation"),
    CommandDefinition(command="memory", description="Manage saved memories", category="memory"),
    CommandDefinition(command="tools", description="View available tools", category="tools"),
    CommandDefinition(command="workflows", description="View active workflows", category="workflows"),
    CommandDefinition(command="integrations", description="View connected integrations", category="integrations"),
    CommandDefinition(command="connect", description="Connect an external service", category="integrations"),
    CommandDefinition(command="disconnect", description="Disconnect an external service", category="integrations"),
    CommandDefinition(command="cancel", description="Cancel an active operation", category="general"),
]

ADMIN_ONLY_COMMANDS: list[CommandDefinition] = [
    CommandDefinition(
        command="health",
        description="Check system health",
        is_admin_only=True,
        category="admin",
    ),
    CommandDefinition(
        command="integration_status",
        description="Check integration status",
        is_admin_only=True,
        category="admin",
    ),
    CommandDefinition(
        command="status",
        description="Display agent running status and uptime",
        is_admin_only=True,
        category="admin",
    ),
    CommandDefinition(
        command="model",
        description="View current Gemini model and provider",
        is_admin_only=True,
        category="admin",
    ),
]


def get_default_user_commands() -> list[CommandDefinition]:
    """Retrieve standard user-facing command definitions."""
    return list(DEFAULT_USER_COMMANDS)


def get_admin_commands() -> list[CommandDefinition]:
    """Retrieve all commands available to administrators (user + admin-only commands)."""
    # Combine user commands with admin commands, preserving order without duplicates
    seen = set()
    combined: list[CommandDefinition] = []
    for cmd in DEFAULT_USER_COMMANDS + ADMIN_ONLY_COMMANDS:
        if cmd.command not in seen:
            seen.add(cmd.command)
            combined.append(cmd)
    return combined


def get_admin_user_ids(config: Any) -> frozenset[int]:
    """Retrieve designated administrator user IDs from configuration."""
    if not config:
        return frozenset()
    admin_ids = getattr(config, "admin_telegram_user_ids", None)
    if admin_ids:
        return frozenset(admin_ids)
    return getattr(config, "allowed_telegram_user_ids", frozenset())


# -----------------------------------------------------------------------------
# Bot Command Setup & Verification
# -----------------------------------------------------------------------------

async def setup_bot_commands(application: Application, force: bool = False) -> bool:
    """Register native Telegram bot commands using setMyCommands for default and admin scopes.

    Configures:
    1. BotCommandScopeDefault: Available to all users tapping the menu or typing '/'.
    2. BotCommandScopeChat: Explicitly registers administrative commands for designated admin users.

    Args:
        application: Active python-telegram-bot Application instance.
        force: Whether to re-register commands even if previously configured in this session.

    Returns:
        True if commands were successfully configured on Telegram, False otherwise.
    """
    bot_data = getattr(application, "bot_data", None)
    if isinstance(bot_data, dict) and bot_data.get("_commands_configured") and not force:
        logger.debug("Bot commands menu already configured for this session.")
        return True

    bot = getattr(application, "bot", None)
    if not bot:
        logger.warning("No bot instance available on application to configure commands.")
        return False

    config = bot_data.get("config") if isinstance(bot_data, dict) else None

    try:
        # 1. Validate command lists
        user_bot_commands = validate_commands_list(get_default_user_commands())
        admin_bot_commands = validate_commands_list(get_admin_commands())

        # 2. Register default commands scope (visible to all users in Telegram menu)
        await bot.set_my_commands(
            commands=user_bot_commands,
            scope=BotCommandScopeDefault(),
        )
        logger.info(
            "Registered %d default commands with BotCommandScopeDefault on Telegram.",
            len(user_bot_commands),
        )

        # 3. Register admin command scopes for designated administrators
        admin_user_ids = get_admin_user_ids(config)
        admin_success_count = 0

        for admin_id in admin_user_ids:
            try:
                await bot.set_my_commands(
                    commands=admin_bot_commands,
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
                admin_success_count += 1
                logger.debug(
                    "Configured %d admin commands for user/chat %s with BotCommandScopeChat.",
                    len(admin_bot_commands),
                    admin_id,
                )
            except Exception as chat_err:
                logger.warning(
                    "Failed configuring admin commands for chat %s: %s",
                    admin_id,
                    chat_err,
                )

        if admin_user_ids:
            logger.info(
                "Registered admin command scopes for %d/%d admin users.",
                admin_success_count,
                len(admin_user_ids),
            )

        if isinstance(bot_data, dict):
            bot_data["_commands_configured"] = True

        logger.info("Native Telegram bot command menus successfully configured and active.")
        return True

    except Exception as exc:
        # Handle Telegram API failures gracefully without crashing application startup
        logger.warning(
            "Failed configuring Telegram bot commands menu via setMyCommands (%s). "
            "Continuing bot execution without native menu.",
            exc,
        )
        return False


async def verify_bot_commands(bot: Any, scope: Any = None) -> list[BotCommand]:
    """Retrieve and verify active commands from Telegram via getMyCommands.

    Args:
        bot: Telegram Bot instance.
        scope: Optional BotCommandScope to verify. Defaults to BotCommandScopeDefault.

    Returns:
        List of BotCommand objects returned by Telegram API.
    """
    target_scope = scope or BotCommandScopeDefault()
    commands = await bot.get_my_commands(scope=target_scope)
    return list(commands)


def format_help_text(is_admin: bool = False) -> str:
    """Generate dynamic formatted /help markdown text based on centralized command definitions."""
    lines = ["🤖 *Personal AI Assistant Commands*", ""]

    lines.append("*General & Conversation:*")
    for cmd in DEFAULT_USER_COMMANDS:
        if cmd.category in ("general", "conversation"):
            lines.append(f"• `/{cmd.command}` - {cmd.description}")

    lines.append("")
    lines.append("*Tools, Memory & Workflows:*")
    for cmd in DEFAULT_USER_COMMANDS:
        if cmd.category in ("tools", "memory", "workflows"):
            lines.append(f"• `/{cmd.command}` - {cmd.description}")

    lines.append("")
    lines.append("*Integrations & Connections:*")
    for cmd in DEFAULT_USER_COMMANDS:
        if cmd.category == "integrations":
            lines.append(f"• `/{cmd.command}` - {cmd.description}")

    if is_admin:
        lines.append("")
        lines.append("*🛠 Admin & Diagnostics:*")
        for cmd in ADMIN_ONLY_COMMANDS:
            lines.append(f"• `/{cmd.command}` - {cmd.description}")

    lines.append("")
    lines.append("💡 _Tap the menu button or type `/` to access commands quickly._")
    lines.append("Just send any text message or document to converse with the agent.")

    return "\n".join(lines).strip()

