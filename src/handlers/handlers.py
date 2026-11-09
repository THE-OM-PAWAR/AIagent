"""Re-export handlers from src.interfaces.telegram.handlers for compatibility."""

from src.interfaces.telegram.handlers import (
    DuplicateMessageFilter,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    clear_command,
    handle_message,
    health_command,
    help_command,
    is_authorized,
    register_handlers,
    send_typing_action,
    split_message,
    start_command,
)

__all__ = [
    "DuplicateMessageFilter",
    "MAX_TELEGRAM_MESSAGE_LENGTH",
    "clear_command",
    "handle_message",
    "health_command",
    "help_command",
    "is_authorized",
    "register_handlers",
    "send_typing_action",
    "split_message",
    "start_command",
]
