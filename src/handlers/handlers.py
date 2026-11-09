"""Re-export handlers from src.interfaces.telegram.handlers for compatibility."""

from src.interfaces.telegram.handlers import (
    DuplicateMessageFilter,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    clear_command,
    handle_message,
    health_command,
    help_command,
    is_authorized,
    model_command,
    register_handlers,
    send_typing_action,
    split_message,
    start_command,
    status_command,
)

__all__ = [
    "DuplicateMessageFilter",
    "MAX_TELEGRAM_MESSAGE_LENGTH",
    "clear_command",
    "handle_message",
    "health_command",
    "help_command",
    "is_authorized",
    "model_command",
    "register_handlers",
    "send_typing_action",
    "split_message",
    "start_command",
    "status_command",
]
