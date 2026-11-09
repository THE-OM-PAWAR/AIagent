"""Telegram bot command and message handlers with authorization, deduplication, and health checks."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
import logging
from collections.abc import AsyncGenerator
import time

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config import Config
from src.core.orchestrator import AgentOrchestrator
from src.database import DatabaseClient
from src.intelligence.providers.gemini import (
    GeminiAPIError,
    GeminiClient,
    GeminiError,
    GeminiRateLimitError,
    GeminiTimeoutError,
)

logger = logging.getLogger(__name__)

# Telegram text messages have a maximum length limit of 4096 characters.
# Using 4000 provides a comfortable safety buffer.
MAX_TELEGRAM_MESSAGE_LENGTH = 4000


class DuplicateMessageFilter:
    """Thread-safe bounded in-memory cache to detect duplicate Telegram update IDs."""

    def __init__(self, capacity: int = 2000) -> None:
        self.capacity = capacity
        self._seen: set[int] = set()
        self._order: deque[int] = deque()
        self._lock = asyncio.Lock()

    async def is_duplicate(self, update_id: int) -> bool:
        """Check if an update_id has already been processed within the cache window.

        Args:
            update_id: Telegram update unique identifier.

        Returns:
            True if the update is duplicate, False otherwise.
        """
        async with self._lock:
            if update_id in self._seen:
                return True
            self._seen.add(update_id)
            self._order.append(update_id)
            if len(self._order) > self.capacity:
                oldest = self._order.popleft()
                self._seen.discard(oldest)
            return False


# Global duplicate filter instance for handlers
duplicate_filter = DuplicateMessageFilter()


def is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Verify whether the sending user is authorized by configuration.

    Args:
        update: Telegram update object.
        context: Telegram callback context.

    Returns:
        True if the user is authorized, False otherwise.
    """
    user = update.effective_user
    config: Config | None = context.bot_data.get("config")
    allowed_ids = config.allowed_telegram_user_ids if config else frozenset()

    if not user or user.id not in allowed_ids:
        logger.warning(
            "Unauthorized access attempt rejected for user_id=%s (username=%s, chat_id=%s)",
            user.id if user else "unknown",
            user.username if user else "none",
            update.effective_chat.id if update.effective_chat else "unknown",
        )
        return False
    return True


def split_message(text: str, max_length: int = MAX_TELEGRAM_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks that satisfy Telegram length restrictions.

    Prioritizes splitting across double newlines, single newlines, and spaces
    to preserve readability.

    Args:
        text: Input string to split.
        max_length: Maximum allowed characters per chunk.

    Returns:
        List of message chunk strings.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Attempt to find paragraph break
        split_point = remaining.rfind("\n\n", 0, max_length)
        if split_point == -1 or split_point < max_length // 2:
            # Attempt to find single newline
            split_point = remaining.rfind("\n", 0, max_length)
        if split_point == -1 or split_point < max_length // 2:
            # Attempt to find space boundary
            split_point = remaining.rfind(" ", 0, max_length)
        if split_point == -1 or split_point < max_length // 2:
            # Fall back to hard boundary
            split_point = max_length

        chunk = remaining[:split_point].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_point:].lstrip()

    return chunks


@asynccontextmanager
async def send_typing_action(
    bot: object,
    chat_id: int,
    interval_seconds: float = 4.0,
) -> AsyncGenerator[None, None]:
    """Provide a continuous typing indicator while an asynchronous task executes.

    Args:
        bot: Telegram Bot instance.
        chat_id: Telegram chat identifier.
        interval_seconds: Frequency to refresh the typing indicator.
    """
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception as exc:
        logger.debug("Failed sending initial typing chat action: %s", exc)

    stop_event = asyncio.Event()

    async def _typing_worker() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                try:
                    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                except Exception as exc:
                    logger.debug("Failed sending recurring typing chat action: %s", exc)

    task = asyncio.create_task(_typing_worker())
    try:
        yield
    finally:
        stop_event.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    user = update.effective_user
    username = user.first_name if user else "there"
    logger.info("Handling /start from authorized user_id=%s", user.id if user else "unknown")

    if user:
        db_client: DatabaseClient | None = context.bot_data.get("db_client")
        if db_client:
            try:
                await db_client.clear_active_conversation(user.id)
            except Exception as exc:
                logger.error("Error clearing conversation on /start for user %s: %s", user.id, exc)

        gemini_client: GeminiClient | None = context.bot_data.get("gemini_client")
        if gemini_client:
            gemini_client.reset_chat(user.id)

    welcome_text = (
        f"👋 Hello {username}!\n\n"
        "I am your personal Telegram AI assistant powered by Google Gemini and persistent MongoDB memory.\n\n"
        "• Send any question or message to chat.\n"
        "• I remember our conversation context even across restarts.\n"
        "• Use /clear to clear current conversation history and start a new topic.\n"
        "• Use /health to check service status.\n"
        "• Use /help to see all available commands."
    )
    if update.message:
        await update.message.reply_text(welcome_text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /help command."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    logger.info("Handling /help command from user_id=%s", update.effective_user.id if update.effective_user else "unknown")
    help_text = (
        "🤖 Personal AI Assistant Commands:\n\n"
        "• /start  - Restart the assistant and begin a clean conversation\n"
        "• /status - Display agent running status, uptime and environment\n"
        "• /health - Check application, MongoDB, and Telegram connectivity\n"
        "• /model  - View current Gemini model and LLM provider\n"
        "• /clear  - Clear active conversation history and start fresh\n"
        "• /reset  - Alias for /clear\n"
        "• /help   - View this help message\n\n"
        "Just send any text message to receive an AI response."
    )
    if update.message:
        await update.message.reply_text(help_text)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /clear or /reset command to archive active conversation context."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return

    logger.info("Handling /clear from user_id=%s", user.id)
    orchestrator: AgentOrchestrator | None = context.bot_data.get("orchestrator")
    if orchestrator:
        await orchestrator.clear_conversation(user.id)
    else:
        db_client: DatabaseClient | None = context.bot_data.get("db_client")
        if db_client:
            try:
                await db_client.clear_active_conversation(user.id)
            except Exception as exc:
                logger.error("Error clearing active conversation for user %s: %s", user.id, exc)

        gemini_client: GeminiClient | None = context.bot_data.get("gemini_client")
        if gemini_client:
            gemini_client.reset_chat(user.id)

    if update.message:
        await update.message.reply_text(
            "🧹 Active conversation context cleared! Previous messages have been archived and a new conversation started."
        )


def format_uptime(start_time: float | None) -> str:
    """Format elapsed seconds since start_time into a concise human-readable string."""
    if not start_time:
        return "Unknown"
    elapsed = max(0, int(time.time() - start_time))
    days, rem = divmod(elapsed, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command to display agent status, uptime and environment."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    logger.info("Handling /status command from user_id=%s", update.effective_user.id if update.effective_user else "unknown")
    config: Config | None = context.bot_data.get("config")
    start_time: float | None = context.bot_data.get("start_time")
    uptime = format_uptime(start_time)
    environment = config.environment if config else "production"

    status_message = (
        "📊 *AIagent System Status*\n\n"
        "• *State:* 🟢 Running\n"
        f"• *Uptime:* {uptime}\n"
        f"• *Environment:* `{environment}`\n"
        f"• *Log Level:* `{config.log_level if config else 'INFO'}`"
    )
    if update.message:
        await update.message.reply_text(status_message, parse_mode="Markdown")


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /health command to report application, database and Telegram connectivity."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    logger.info("Handling /health command from user_id=%s", update.effective_user.id if update.effective_user else "unknown")
    db_client: DatabaseClient | None = context.bot_data.get("db_client")
    config: Config | None = context.bot_data.get("config")

    mongo_status = "Disconnected"
    latency_ms = 0.0

    if db_client:
        try:
            start_t = time.perf_counter()
            await db_client.db.command("ping")
            latency_ms = (time.perf_counter() - start_t) * 1000
            mongo_status = f"🟢 Connected ({latency_ms:.1f}ms)"
        except Exception as exc:
            mongo_status = f"🔴 Error: {exc.__class__.__name__}"

    # Telegram API connectivity check
    telegram_status = "🔴 Unknown"
    try:
        if context.bot:
            bot_user = await context.bot.get_me()
            bot_handle = f" (@{bot_user.username})" if bot_user and bot_user.username else ""
            telegram_status = f"🟢 Connected{bot_handle}"
        else:
            telegram_status = "🟢 Connected"
    except Exception as exc:
        telegram_status = f"🔴 Error: {exc.__class__.__name__}"

    model_name = config.gemini_model if config else "Unknown"
    allowed_count = len(config.allowed_telegram_user_ids) if config else 0
    app_state = "🟢 Operational" if "🟢" in mongo_status else "⚠️ Degraded"

    status_report = (
        "🏥 *System Health Status*\n\n"
        f"• *Application:* {app_state}\n"
        f"• *MongoDB:* {mongo_status}\n"
        f"• *Telegram API:* {telegram_status}\n"
        f"• *Status:* Operational\n"
        f"• *Gemini Model:* `{model_name}`\n"
        f"• *Authorized Users:* {allowed_count}\n"
        f"• *Environment:* `{config.environment if config else 'production'}`"
    )
    if update.message:
        await update.message.reply_text(status_report, parse_mode="Markdown")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model command to display configured Gemini model and provider details."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    logger.info("Handling /model command from user_id=%s", update.effective_user.id if update.effective_user else "unknown")
    config: Config | None = context.bot_data.get("config")

    model_name = config.gemini_model if config else "Unknown"
    timeout = config.gemini_timeout_seconds if config else 60.0
    history_limit = config.conversation_history_limit if config else 20
    sys_instruction = config.gemini_system_instruction if config else "None configured"

    # Truncate exceptionally long system instruction for clean Telegram display
    if len(sys_instruction) > 250:
        display_sys = sys_instruction[:247] + "..."
    else:
        display_sys = sys_instruction

    model_message = (
        "🧠 *AI Model Configuration*\n\n"
        "• *Provider:* Google Gemini (Official GenAI SDK)\n"
        f"• *Model:* `{model_name}`\n"
        f"• *Request Timeout:* {timeout:.1f}s\n"
        f"• *Context Window Limit:* {history_limit} messages\n\n"
        f"📝 *System Instruction:*\n_{display_sys}_"
    )
    if update.message:
        await update.message.reply_text(model_message, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages with authorization, deduplication, and persistence."""
    # 1. Authorization check
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    # 2. Duplicate update check
    if update.update_id is not None:
        if await duplicate_filter.is_duplicate(update.update_id):
            logger.debug("Skipping duplicate update_id=%s", update.update_id)
            return

    message = update.message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not message.text or not user or not chat:
        return

    user_text = message.text.strip()
    if not user_text:
        return

    # 3. Resolve or initialize Agent Orchestrator
    orchestrator: AgentOrchestrator | None = context.bot_data.get("orchestrator")
    if not orchestrator:
        gemini_client: GeminiClient | None = context.bot_data.get("gemini_client")
        if not gemini_client:
            logger.error("Neither orchestrator nor GeminiClient found in context.bot_data.")
            await message.reply_text("⚠️ AI service is temporarily unavailable. Please try again later.")
            return

        db_client: DatabaseClient | None = context.bot_data.get("db_client")
        config: Config | None = context.bot_data.get("config")
        history_limit = config.conversation_history_limit if config else 20

        orchestrator = AgentOrchestrator(
            gemini_client=gemini_client,
            db_client=db_client,
            context_limit=history_limit,
        )
        context.bot_data["orchestrator"] = orchestrator

    # Log sanitized message metadata without logging sensitive text content
    logger.info(
        "Telegram: receiving message from user_id=%s (length=%d, msg_id=%s, chat_id=%s)",
        user.id,
        len(user_text),
        message.message_id,
        chat.id,
    )

    try:
        # Show typing indicator while orchestrating response
        async with send_typing_action(context.bot, chat.id):
            response_text = await orchestrator.process_message(
                user_id=user.id,
                user_text=user_text,
                update_id=update.update_id,
            )

        # Split and deliver response chunks complying with Telegram message length limits
        chunks = split_message(response_text)
        for chunk in chunks:
            await message.reply_text(chunk)

    except GeminiRateLimitError:
        logger.warning("Rate limit encountered for user %s", user.id)
        await message.reply_text(
            "⏳ The Gemini API rate limit has been reached. Please wait a moment before sending another message."
        )
    except GeminiTimeoutError:
        logger.warning("Gemini timeout encountered for user %s", user.id)
        await message.reply_text(
            "⏱️ The request to Gemini timed out. Please try asking again."
        )
    except GeminiAPIError as err:
        logger.error("Gemini API error for user %s: %s", user.id, err)
        await message.reply_text(
            "⚠️ An error occurred while generating the response. Please try again later."
        )
    except GeminiError as err:
        logger.error("Gemini service error for user %s: %s", user.id, err)
        await message.reply_text(
            "⚠️ Service error encountered while communicating with Gemini."
        )
    except Exception as exc:
        logger.error("Unexpected error handling message for user %s: %s", user.id, exc, exc_info=True)
        await message.reply_text("⚠️ An unexpected error occurred. Please try again.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by incoming Telegram updates."""
    logger.error("Exception occurred while handling an update: %s", context.error, exc_info=context.error)


def register_handlers(application: Application) -> None:
    """Register all command and message handlers to the Telegram application."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("health", health_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("reset", clear_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    logger.info("Telegram command and message handlers registered.")
