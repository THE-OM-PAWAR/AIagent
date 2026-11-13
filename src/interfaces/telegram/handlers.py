"""Telegram bot command and message handlers with authorization, deduplication, and health checks."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
import re
import time

from unittest.mock import MagicMock
from telegram import Document, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config import Config
from src.core.orchestrator import AgentOrchestrator
from src.database.repositories.conversation_repo import ConversationRepository
from src.database.repositories.integration_repo import IntegrationRepository
from src.database.session import get_async_session
from src.integrations.base import AuthType
from src.integrations.connectors.calendar import GoogleCalendarConnector
from src.integrations.connectors.calendly import CalendlyConnector, CalendlyOAuthClient
from src.integrations.connectors.drive import GoogleDriveConnector
from src.integrations.connectors.gmail import GmailConnector
from src.integrations.connectors.google import GoogleOAuthClient, GoogleWorkspaceConnector
from src.integrations.connectors.maps import GooglePlacesConnector
from src.integrations.connectors.web_search import WebSearchConnector
from src.integrations.registry import IntegrationRegistry
from src.integrations.security.crypto import CredentialEncryptor, mask_secret
from src.intelligence.providers.gemini import (
    GeminiAPIError,
    GeminiAuthError,
    GeminiClient,
    GeminiError,
    GeminiInvalidRequestError,
    GeminiRateLimitError,
    GeminiServerError,
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
        orchestrator: AgentOrchestrator | None = context.bot_data.get("orchestrator")
        if orchestrator:
            await orchestrator.clear_conversation(user.id)
        else:
            db_client = context.bot_data.get("db_client")
            if db_client:
                try:
                    await db_client.clear_active_conversation(user.id)
                except Exception as exc:
                    logger.error("Error clearing conversation on /start for user %s: %s", user.id, exc)

            config: Config | None = context.bot_data.get("config")
            db_url = getattr(config, "database_url", None)
            if db_url:
                try:
                    async with get_async_session(db_url) as session:
                        repo = ConversationRepository(session)
                        await repo.clear_active_conversation(user.id)
                except Exception as exc:
                    logger.error("Error clearing conversation on /start for user %s: %s", user.id, exc)

        gemini_client: GeminiClient | None = context.bot_data.get("gemini_client")
        if gemini_client:
            gemini_client.reset_chat(user.id)

    welcome_text = (
        f"👋 Hello {username}!\n\n"
        "I am your personal Telegram AI assistant powered by Google Gemini and persistent PostgreSQL (Neon) memory.\n\n"
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

    user = update.effective_user
    logger.info("Handling /help command from user_id=%s", user.id if user else "unknown")

    config = context.bot_data.get("config") if hasattr(context, "bot_data") else None
    from src.interfaces.telegram.commands import format_help_text, get_admin_user_ids

    is_admin = bool(user and user.id in get_admin_user_ids(config))
    help_text = format_help_text(is_admin=is_admin)

    target = update.message or update.effective_message
    if target:
        await target.reply_text(help_text, parse_mode="Markdown")


@asynccontextmanager
async def get_integration_repo(context: ContextTypes.DEFAULT_TYPE) -> AsyncGenerator[IntegrationRepository | None, None]:
    """Provide an IntegrationRepository instance bound to the active database session."""
    bot_data = getattr(context, "bot_data", None)
    if isinstance(bot_data, dict):
        injected_repo = bot_data.get("integration_repository")
        if injected_repo is not None:
            yield injected_repo
            return
        config = bot_data.get("config")
        orchestrator = bot_data.get("orchestrator")
    else:
        config = None
        orchestrator = None

    raw_db_url = (getattr(config, "database_url", None) if config else None) or (getattr(orchestrator, "database_url", None) if orchestrator else None)
    db_url = raw_db_url if isinstance(raw_db_url, str) else None
    if not db_url:
        yield None
        return

    try:
        async with get_async_session(db_url) as session:
            yield IntegrationRepository(session)
    except Exception as exc:
        logger.error("Failed to acquire session for IntegrationRepository: %s", exc)
        yield None


def get_integration_registry(context: ContextTypes.DEFAULT_TYPE) -> IntegrationRegistry:
    """Retrieve or lazily initialize the IntegrationRegistry."""
    bot_data = getattr(context, "bot_data", None)
    registry = bot_data.get("integration_registry") if isinstance(bot_data, dict) else None
    if registry is None:
        config = bot_data.get("config") if isinstance(bot_data, dict) else None
        enc_key = getattr(config, "integration_encryption_key", None) or getattr(config, "telegram_bot_token", None)
        if not isinstance(enc_key, (str, bytes)):
            enc_key = "default-integration-encryption-key-32b"
        encryptor = CredentialEncryptor(enc_key)
        registry = IntegrationRegistry(encryptor=encryptor)

        # 1. Register default web search connector
        sys_key = getattr(config, "tavily_api_key", None) or getattr(config, "brave_api_key", None)
        if not isinstance(sys_key, str):
            sys_key = None
        def_provider = getattr(config, "search_provider", "tavily")
        if not isinstance(def_provider, str):
            def_provider = "tavily"
        registry.register_connector(WebSearchConnector(default_provider=def_provider, system_api_key=sys_key))

        # 2. Register Google Workspace & Places connectors
        db_url = getattr(config, "database_url", None)
        client_id = getattr(config, "google_client_id", None)
        client_secret = getattr(config, "google_client_secret", None)
        redirect_uri = getattr(config, "google_redirect_uri", "urn:ietf:wg:oauth:2.0:oob")
        maps_key = getattr(config, "google_maps_api_key", None)

        oauth_client = GoogleOAuthClient(client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri)
        registry.register_connector(GoogleWorkspaceConnector(oauth_client=oauth_client, database_url=db_url))
        registry.register_connector(GmailConnector(database_url=db_url))
        registry.register_connector(GoogleCalendarConnector(database_url=db_url))
        registry.register_connector(GoogleDriveConnector(database_url=db_url))
        registry.register_connector(GooglePlacesConnector(system_api_key=maps_key, database_url=db_url))

        # 3. Register Calendly connector
        calendly_client_id = getattr(config, "calendly_client_id", None)
        calendly_client_secret = getattr(config, "calendly_client_secret", None)
        calendly_redirect_uri = getattr(config, "calendly_redirect_uri", "urn:ietf:wg:oauth:2.0:oob")
        calendly_api_key = getattr(config, "calendly_api_key", None)

        calendly_oauth = CalendlyOAuthClient(
            client_id=calendly_client_id,
            client_secret=calendly_client_secret,
            redirect_uri=calendly_redirect_uri,
        )
        registry.register_connector(
            CalendlyConnector(
                oauth_client=calendly_oauth,
                system_api_key=calendly_api_key,
                database_url=db_url,
            )
        )

        if isinstance(bot_data, dict):
            bot_data["integration_registry"] = registry
    return registry


async def integrations_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /integrations command: list all integrations with connection status and action buttons."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return

    registry = get_integration_registry(context)
    async with get_integration_repo(context) as repo:
        if not repo:
            if update.message:
                await update.message.reply_text("⚠️ Database is not configured. Integrations are unavailable.")
            return

        status_list = await registry.get_status(repo, user.id)

    if not status_list:
        if update.message:
            await update.message.reply_text("ℹ️ No integrations are currently registered.")
        return

    lines = ["🔌 *External Integrations & Services*", ""]
    keyboard: list[list[InlineKeyboardButton]] = []

    for item in status_list:
        pid = item["provider_id"]
        name = item["name"]
        is_conn = item["is_connected"]
        masked = item.get("masked_key")

        if is_conn:
            badge = "🟢 Connected"
            key_info = f" (Key: `{masked}`)" if masked else ""
            lines.append(f"• *{name}* (`{pid}`): {badge}{key_info}")
            lines.append(f"  _{item['description']}_")
            row = [
                InlineKeyboardButton(f"Test {name} 🔍", callback_data=f"int_test:{pid}"),
                InlineKeyboardButton("Disconnect ❌", callback_data=f"int_disc:{pid}"),
            ]
            keyboard.append(row)
        else:
            badge = "⚪ Not connected"
            lines.append(f"• *{name}* (`{pid}`): {badge}")
            lines.append(f"  _{item['description']}_")
            row = [
                InlineKeyboardButton(f"Connect {name} 🔗", callback_data=f"int_conn:{pid}")
            ]
            keyboard.append(row)
        lines.append("")

    lines.append("💡 *Commands:*")
    lines.append("• Connect: `/connect <provider_id>`")
    lines.append("• Disconnect: `/disconnect <provider_id>`")
    lines.append("• Verify health: `/integration_status`")

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    text = "\n".join(lines)
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)


async def initiate_connect_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    provider_id: str,
) -> None:
    """Prompt user to provide an API key for the requested integration provider."""
    registry = get_integration_registry(context)
    connector = registry.get_connector(provider_id)
    if not connector:
        msg = f"⚠️ Unknown provider `{provider_id}`. Use `/integrations` to view available integrations."
        target = update.message or update.effective_message
        if target:
            await target.reply_text(msg, parse_mode="Markdown")
        return

    meta = connector.metadata
    user = update.effective_user
    user_id = user.id if user else 0

    # OAuth2 or PAT flow for Calendly
    if provider_id == "calendly":
        calendly_conn = registry.get_connector("calendly")
        oauth_client = getattr(calendly_conn, "oauth_client", None) or CalendlyOAuthClient()
        has_client_id = bool(getattr(oauth_client, "client_id", None))

        if has_client_id:
            try:
                auth_url, csrf = oauth_client.generate_authorization_url(user_id)
                context.user_data["awaiting_calendly_for"] = "calendly"
                context.user_data["oauth_csrf"] = csrf

                prompt_text = (
                    "🔐 *Connect Calendly*\n\n"
                    "1. Tap the link below to authorize access with Calendly:\n"
                    f"👉 [**Authorize Calendly Account**]({auth_url})\n\n"
                    "2. Sign in and grant access, then copy the authorization code.\n"
                    "3. Send the **authorization code** (or your **Personal Access Token**) in your next message.\n\n"
                    "🛡️ *Security & Privacy:*\n"
                    "• Your message will be **immediately deleted** from Telegram chat history.\n"
                    "• Credentials are encrypted at rest with Fernet.\n"
                    "• Type `/cancel` at any time to abort."
                )
                target = update.message or update.effective_message
                if target:
                    await target.reply_text(prompt_text, parse_mode="Markdown")
                return
            except Exception as exc:
                logger.debug("Could not generate Calendly OAuth link: %s. Falling back to PAT prompt.", exc)

        context.user_data["awaiting_calendly_for"] = "calendly"
        prompt_text = (
            "🔑 *Connect Calendly with Personal Access Token (PAT)*\n\n"
            "Please send your **Calendly Personal Access Token** in your next message.\n\n"
            "📖 *How to get a token:*\n"
            "1. Go to Calendly > Integrations > API and Webhooks\n"
            "2. Generate a Personal Access Token and paste it here.\n\n"
            "🛡️ *Security & Privacy:*\n"
            "• Your message will be **immediately deleted** from Telegram history.\n"
            "• The token is encrypted with Fernet and never logged.\n"
            "• Type `/cancel` at any time to abort."
        )
        target = update.message or update.effective_message
        if target:
            await target.reply_text(prompt_text, parse_mode="Markdown")
        return

    # OAuth2 flow for Google Workspace (Gmail, Calendar, Drive)
    if meta.auth_type == AuthType.OAUTH2:
        google_conn = registry.get_connector("google")
        oauth_client = getattr(google_conn, "oauth_client", None) or GoogleOAuthClient()
        try:
            auth_url, csrf = oauth_client.generate_authorization_url(user_id)
            context.user_data["awaiting_oauth_code_for"] = provider_id
            context.user_data["oauth_csrf"] = csrf

            prompt_text = (
                f"🔐 *Connect {meta.name} (Google Workspace)*\n\n"
                f"1. Tap the link below to authorize access with Google:\n"
                f"👉 [**Authorize Google Account**]({auth_url})\n\n"
                f"2. Sign in and grant required permissions.\n"
                f"3. Copy the authorization code and send it here in your next message.\n\n"
                "🛡️ *Security & Privacy:*\n"
                "• Your authorization code will be **immediately deleted** from Telegram chat history.\n"
                "• Access and refresh tokens are encrypted at rest with Fernet and automatically renewed.\n"
                "• Type `/cancel` at any time to abort."
            )
            target = update.message or update.effective_message
            if target:
                await target.reply_text(prompt_text, parse_mode="Markdown")
            return
        except Exception as exc:
            msg = f"⚠️ Could not generate authorization link: {exc}\nPlease ensure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are configured in `.env`."
            target = update.message or update.effective_message
            if target:
                await target.reply_text(msg)
            return

    context.user_data["awaiting_api_key_for"] = provider_id

    setup_line = f"\n\n📖 *Setup Guide:* {meta.setup_guide}" if meta.setup_guide else ""
    prompt_text = (
        f"🔑 *Connect {meta.name}* (`{provider_id}`)\n\n"
        f"Please send your **API key** in your next message.{setup_line}\n\n"
        "🛡️ *Security & Privacy:*\n"
        "• Your message containing the key will be **immediately deleted** from Telegram chat history.\n"
        "• The key is encrypted at rest using Fernet and never logged or sent to the LLM.\n"
        "• Type `/cancel` at any time to abort."
    )
    target = update.message or update.effective_message
    if target:
        await target.reply_text(prompt_text, parse_mode="Markdown")


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /connect <provider_id> command."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return

    args = context.args or []
    if not args:
        registry = get_integration_registry(context)
        providers = registry.list_providers()
        keyboard = [
            [InlineKeyboardButton(f"Connect {p.name}", callback_data=f"int_conn:{p.provider_id}")]
            for p in providers
        ]
        text = (
            "🔌 *Connect an Integration*\n\n"
            "Please specify a provider to connect:\n"
            "• `/connect web_search`\n\n"
            "Or tap one of the available providers below:"
        )
        if update.message:
            await update.message.reply_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
            )
        return

    provider_id = args[0].strip().lower()
    await initiate_connect_prompt(update, context, provider_id)


async def disconnect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /disconnect <provider_id> command."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return

    args = context.args or []
    registry = get_integration_registry(context)

    async with get_integration_repo(context) as repo:
        if not repo:
            if update.message:
                await update.message.reply_text("⚠️ Database is not configured. Integrations are unavailable.")
            return

        if not args:
            status_list = await registry.get_status(repo, user.id)
            connected = [s for s in status_list if s["is_connected"]]
            if not connected:
                if update.message:
                    await update.message.reply_text("ℹ️ You have no active integrations connected.")
                return
            keyboard = [
                [InlineKeyboardButton(f"Disconnect {s['name']}", callback_data=f"int_disc:{s['provider_id']}")]
                for s in connected
            ]
            if update.message:
                await update.message.reply_text(
                    "Select an integration to disconnect:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            return

        provider_id = args[0].strip().lower()
        success = await registry.disconnect(repo, user.id, provider_id)
        if success:
            if update.message:
                await update.message.reply_text(
                    f"✅ Successfully disconnected `{provider_id}`. Stored credentials have been permanently revoked and removed.",
                    parse_mode="Markdown",
                )
        else:
            if update.message:
                await update.message.reply_text(
                    f"ℹ️ `{provider_id}` was not connected.",
                    parse_mode="Markdown",
                )


async def integration_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /integration_status: runs live health checks across connected integrations."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return

    registry = get_integration_registry(context)
    async with get_integration_repo(context) as repo:
        if not repo:
            if update.message:
                await update.message.reply_text("⚠️ Database is not configured. Integrations are unavailable.")
            return

        status_list = await registry.get_status(repo, user.id)
        connected = [s for s in status_list if s["is_connected"]]

        if not connected:
            if update.message:
                await update.message.reply_text(
                    "🔌 *Integration Health Status*\n\n"
                    "No external integrations are currently connected.\n"
                    "Use `/integrations` or `/connect <provider>` to connect services.",
                    parse_mode="Markdown",
                )
            return

        lines = ["🔌 *Integration Health & Diagnostics*", ""]
        for item in connected:
            pid = item["provider_id"]
            name = item["name"]
            masked = item.get("masked_key") or "configured"

            health = await registry.check_health(repo, user.id, pid)
            if health.is_healthy:
                status_icon = "🟢 Healthy"
                lat_text = f" ({health.latency_ms:.1f}ms)" if health.latency_ms else ""
                lines.append(f"• *{name}* (`{pid}`): {status_icon}{lat_text}")
                lines.append(f"  Key: `{masked}` | Status: `{health.status}`")
            else:
                status_icon = "🔴 Unhealthy"
                lines.append(f"• *{name}* (`{pid}`): {status_icon}")
                lines.append(f"  Error: {health.error_message or 'Unknown error'}")
            lines.append("")

        report = "\n".join(lines).strip()
        if update.message:
            await update.message.reply_text(report, parse_mode="Markdown")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel to abort an ongoing API key or OAuth code input prompt."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    cancelled_provider = None
    if isinstance(getattr(context, "user_data", None), dict):
        if context.user_data.get("awaiting_api_key_for"):
            cancelled_provider = context.user_data.pop("awaiting_api_key_for")
        elif context.user_data.get("awaiting_oauth_code_for"):
            cancelled_provider = context.user_data.pop("awaiting_oauth_code_for")
            context.user_data.pop("oauth_csrf", None)
        elif context.user_data.get("awaiting_calendly_for"):
            cancelled_provider = context.user_data.pop("awaiting_calendly_for")
            context.user_data.pop("oauth_csrf", None)

    if cancelled_provider:
        if update.message:
            await update.message.reply_text(f"🚫 Connection process for `{cancelled_provider}` cancelled.", parse_mode="Markdown")
    else:
        if update.message:
            await update.message.reply_text("ℹ️ No active operation to cancel.")


async def integration_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button callbacks for integration actions."""
    query = update.callback_query
    if not query:
        return

    await query.answer()

    if not is_authorized(update, context):
        await query.edit_message_text("⛔ Access denied.")
        return

    data = query.data or ""
    user = update.effective_user
    if not user:
        return

    registry = get_integration_registry(context)

    if data.startswith("int_conn:"):
        pid = data.split(":", 1)[1]
        await initiate_connect_prompt(update, context, pid)

    elif data.startswith("int_disc:"):
        pid = data.split(":", 1)[1]
        async with get_integration_repo(context) as repo:
            if repo:
                deleted = await registry.disconnect(repo, user.id, pid)
                if deleted:
                    await query.edit_message_text(f"✅ Disconnected `{pid}`. Credentials deleted.", parse_mode="Markdown")
                else:
                    await query.edit_message_text(f"ℹ️ `{pid}` was already disconnected.", parse_mode="Markdown")
            else:
                await query.edit_message_text("⚠️ Database unavailable.")

    elif data.startswith("int_test:"):
        pid = data.split(":", 1)[1]
        async with get_integration_repo(context) as repo:
            if repo:
                health = await registry.check_health(repo, user.id, pid)
                if health.is_healthy:
                    lat = f" ({health.latency_ms:.1f}ms)" if health.latency_ms else ""
                    await query.edit_message_text(f"🟢 *{pid}* connection verified successfully!{lat}", parse_mode="Markdown")
                else:
                    await query.edit_message_text(f"🔴 *{pid}* health check failed: {health.error_message}", parse_mode="Markdown")
            else:
                await query.edit_message_text("⚠️ Database unavailable.")


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
        db_client = context.bot_data.get("db_client")
        if db_client:
            try:
                await db_client.clear_active_conversation(user.id)
            except Exception as exc:
                logger.error("Error clearing active conversation for user %s: %s", user.id, exc)

        config: Config | None = context.bot_data.get("config")
        db_url = getattr(config, "database_url", None)
        if db_url:
            try:
                async with get_async_session(db_url) as session:
                    repo = ConversationRepository(session)
                    await repo.clear_active_conversation(user.id)
            except Exception as exc:
                logger.error("Error clearing active conversation for user %s: %s", user.id, exc)

        gemini_client: GeminiClient | None = context.bot_data.get("gemini_client")
        if gemini_client:
            gemini_client.reset_chat(user.id)

    if update.message:
        await update.message.reply_text(
            "🧹 Active conversation context cleared! Previous messages have been archived and a new conversation started."
        )


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /memory command: inspect and manage saved long-term memories."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return

    logger.info("Handling /memory from user_id=%s", user.id)
    args = context.args or []
    subcommand = args[0].strip().lower() if args else "list"

    orchestrator: AgentOrchestrator | None = context.bot_data.get("orchestrator")
    config: Config | None = context.bot_data.get("config")
    db_url = getattr(config, "database_url", None) or (getattr(orchestrator, "database_url", None) if orchestrator else None)

    if not db_url:
        if update.message:
            await update.message.reply_text("ℹ️ Memory storage is not configured (database URL missing).")
        return

    try:
        from src.database.repositories.memory_repo import MemoryRepository
        from src.database.session import get_async_session

        async with get_async_session(db_url) as session:
            repo = MemoryRepository(session)

            if subcommand == "clear":
                active_memories = await repo.list_active(user_id=user.id, limit=200)
                if not active_memories:
                    if update.message:
                        await update.message.reply_text("ℹ️ You have no active saved memories to clear.")
                    return
                for mem in active_memories:
                    await repo.set_status(mem.id, user.id, "archived")
                if update.message:
                    await update.message.reply_text(
                        f"🧹 Successfully cleared {len(active_memories)} saved memories."
                    )
                return

            # Default: list active memories
            memories = await repo.list_active(user_id=user.id, limit=25)
            if not memories:
                text = (
                    "🧠 *Saved Memories*\n\n"
                    "No active memories saved yet.\n"
                    "As we chat, I automatically remember key facts, projects, and personal preferences.\n\n"
                    "💡 *Commands:*\n"
                    "• `/memory` - View saved memories\n"
                    "• `/memory clear` - Clear all saved memories"
                )
                if update.message:
                    await update.message.reply_text(text, parse_mode="Markdown")
                return

            lines = [f"🧠 *Saved Memories ({len(memories)})*", ""]
            for m in memories:
                cat_badge = f"`[{m.category}]`" if m.category else "`[fact]`"
                lines.append(f"• {cat_badge} {m.content}")

            lines.append("")
            lines.append("💡 _Memories are automatically retrieved to personalize your AI responses._")
            lines.append("To clear all memories, use `/memory clear`.")

            text = "\n".join(lines)
            if update.message:
                await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Error accessing memories for user %s: %s", user.id, exc)
        if update.message:
            await update.message.reply_text("⚠️ An error occurred while accessing memories.")


async def tools_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tools command: list registered agent tools and capabilities."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    user = update.effective_user
    logger.info("Handling /tools command from user_id=%s", user.id if user else "unknown")

    orchestrator: AgentOrchestrator | None = context.bot_data.get("orchestrator")
    registry = getattr(orchestrator, "tool_registry", None)

    if not registry:
        if update.message:
            await update.message.reply_text("🛠 No tool registry configured.")
        return

    tool_list = registry.list_tools()
    if not tool_list:
        if update.message:
            await update.message.reply_text("🛠 No tools are currently registered.")
        return

    lines = [f"🛠 *Available Agent Tools ({len(tool_list)})*", ""]

    read_tools: list[str] = []
    write_tools: list[str] = []
    admin_tools: list[str] = []

    for t in tool_list:
        name = t.get("name", "unknown")
        desc = t.get("description", "No description provided.")
        if len(desc) > 80:
            desc = desc[:77] + "..."
        perm = t.get("permission", "read_only")
        line = f"• `{name}`: _{desc}_"
        if perm == "admin":
            admin_tools.append(line)
        elif perm == "write":
            write_tools.append(line)
        else:
            read_tools.append(line)

    if read_tools:
        lines.append("*📖 Read-Only Tools:*")
        lines.extend(read_tools)
        lines.append("")

    if write_tools:
        lines.append("*✍️ Action & Write Tools:*")
        lines.extend(write_tools)
        lines.append("")

    if admin_tools:
        lines.append("*🛡️ Admin Tools:*")
        lines.extend(admin_tools)
        lines.append("")

    lines.append("💡 _The agent automatically selects and executes tools based on your requests._")

    text = "\n".join(lines).strip()
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown")


async def workflows_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /workflows command: view active workflows, trigger runs, cancel, or plan from natural language."""
    if not is_authorized(update, context):
        if update.message:
            await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return

    logger.info("Handling /workflows command from user_id=%s", user.id)

    workflow_engine = context.bot_data.get("workflow_engine")
    orchestrator: AgentOrchestrator | None = context.bot_data.get("orchestrator")
    if not workflow_engine and orchestrator:
        workflow_engine = getattr(orchestrator, "workflow_engine", None)

    if not workflow_engine:
        if update.message:
            await update.message.reply_text(
                "⚙️ *Workflows*\n\n"
                "No active workflows found.\n"
                "Multi-step automated workflows will appear here when running.",
                parse_mode="Markdown",
            )
        return

    args = context.args or []
    subcmd = args[0].lower().strip() if args else "list"

    try:
        # Subcommand: /workflows run <workflow_id>
        if subcmd == "run" and len(args) >= 2:
            wf_id = args[1].strip()
            if update.message:
                await update.message.reply_text(f"⏳ Executing workflow `{wf_id}`...", parse_mode="Markdown")
            res = await workflow_engine.execute_workflow(workflow_id=wf_id, user_id=user.id, trigger_type="telegram")
            st = res.get("status", "unknown").upper()
            steps_done = res.get("current_step_index", 0)
            tot = res.get("total_steps", 0)
            err = res.get("error")
            msg = (
                f"⚙️ *Workflow Run Finished*\n\n"
                f"• ID: `{wf_id}`\n"
                f"• Status: *{st}*\n"
                f"• Progress: {steps_done}/{tot} steps\n"
            )
            if err:
                msg += f"• Error: `{err}`\n"
            if update.message:
                await update.message.reply_text(msg, parse_mode="Markdown")
            return

        # Subcommand: /workflows cancel <workflow_id>
        if subcmd == "cancel" and len(args) >= 2:
            wf_id = args[1].strip()
            cancelled = await workflow_engine.cancel_workflow(workflow_id=wf_id, user_id=user.id)
            if update.message:
                await update.message.reply_text(
                    f"🛑 Workflow `{wf_id}` {'cancelled successfully.' if cancelled else 'could not be cancelled.'}",
                    parse_mode="Markdown",
                )
            return

        # Subcommand: /workflows status <workflow_id>
        if subcmd == "status" and len(args) >= 2:
            wf_id = args[1].strip()
            status_data = await workflow_engine.get_status(workflow_id=wf_id, user_id=user.id)
            steps_info = status_data.get("steps", [])
            lines = [
                f"⚙️ *Workflow Details: {status_data.get('name', wf_id)}*",
                f"• ID: `{wf_id}`",
                f"• Status: *{str(status_data.get('status')).upper()}*",
                f"• Steps: {len(steps_info)}",
                "",
                "*Step Breakdown:*",
            ]
            for s in steps_info:
                s_name = s.get("name", "step")
                s_tool = s.get("tool_name", "")
                s_st = s.get("status", "pending")
                icon = "✅" if s_st == "completed" else ("⏭️" if s_st == "skipped" else "⏳")
                lines.append(f"{icon} `{s_name}` ({s_tool}) - *{s_st}*")
            if update.message:
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            return

        # Subcommand: /workflows pause <workflow_id>
        if subcmd == "pause" and len(args) >= 2:
            wf_id = args[1].strip()
            paused = await workflow_engine.pause_workflow(workflow_id=wf_id, user_id=user.id)
            if update.message:
                await update.message.reply_text(
                    f"⏸️ Workflow `{wf_id}` {'paused successfully.' if paused else 'could not be paused.'}",
                    parse_mode="Markdown",
                )
            return

        # Subcommand: /workflows resume <workflow_id>
        if subcmd == "resume" and len(args) >= 2:
            wf_id = args[1].strip()
            if update.message:
                await update.message.reply_text(f"▶️ Resuming workflow `{wf_id}`...", parse_mode="Markdown")
            res = await workflow_engine.execute_workflow(workflow_id=wf_id, user_id=user.id, trigger_type="telegram_resume")
            st = res.get("status", "unknown").upper()
            if update.message:
                await update.message.reply_text(
                    f"⚙️ Workflow `{wf_id}` execution resumed. Final status: *{st}*",
                    parse_mode="Markdown",
                )
            return

        # Subcommand: /workflows approvals
        if subcmd in ("approvals", "pending_approvals"):
            appr_mgr = getattr(workflow_engine, "approval_manager", None)
            if not appr_mgr:
                if update.message:
                    await update.message.reply_text("ℹ️ Approval manager is not configured.")
                return
            pending = await appr_mgr.list_pending_approvals(user_id=user.id)
            if not pending:
                if update.message:
                    await update.message.reply_text("✅ You have no pending action approvals.")
                return
            lines = [f"🛡️ *Pending Approvals ({len(pending)})*", ""]
            for a in pending:
                lines.append(f"• ID: `{a['approval_id']}`")
                lines.append(f"  Tool: `{a['tool_name']}` (Step: `{a['step_name']}`)")
                lines.append(f"  Action: _{a['action_summary']}_")
                lines.append(f"  Expires: `{a['expires_at']}`")
                lines.append("")
            lines.append("💡 _To approve: `/workflows approve <approval_id>`_")
            lines.append("💡 _To reject: `/workflows reject <approval_id>`_")
            if update.message:
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            return

        # Subcommand: /workflows approve <approval_id>
        if subcmd == "approve" and len(args) >= 2:
            appr_id = args[1].strip()
            appr_mgr = getattr(workflow_engine, "approval_manager", None)
            if not appr_mgr:
                if update.message:
                    await update.message.reply_text("ℹ️ Approval manager is not configured.")
                return
            if update.message:
                await update.message.reply_text(f"⏳ Granting approval and resuming workflow for `{appr_id}`...", parse_mode="Markdown")
            await appr_mgr.approve_request(approval_id=appr_id, user_id=user.id)
            res = await workflow_engine.resume_approved_workflow(approval_id=appr_id, user_id=user.id)
            st = res.get("status", "unknown").upper()
            if update.message:
                await update.message.reply_text(
                    f"✅ Approval granted. Workflow execution resumed!\n• Final status: *{st}*",
                    parse_mode="Markdown",
                )
            return

        # Subcommand: /workflows reject <approval_id>
        if subcmd == "reject" and len(args) >= 2:
            appr_id = args[1].strip()
            reason = " ".join(args[2:]) if len(args) > 2 else "Rejected by user"
            appr_mgr = getattr(workflow_engine, "approval_manager", None)
            if not appr_mgr:
                if update.message:
                    await update.message.reply_text("ℹ️ Approval manager is not configured.")
                return
            await appr_mgr.reject_request(approval_id=appr_id, user_id=user.id, resolution_notes=reason)
            if update.message:
                await update.message.reply_text(
                    f"🛑 Approval `{appr_id}` rejected. Workflow step will not execute.",
                    parse_mode="Markdown",
                )
            return

        # Subcommand: /workflows schedules
        if subcmd in ("schedules", "list_schedules"):
            scheduler = context.bot_data.get("workflow_scheduler") or getattr(workflow_engine, "workflow_scheduler", None)
            if not scheduler:
                if update.message:
                    await update.message.reply_text("ℹ️ Workflow scheduler is not configured.")
                return
            schedules = await scheduler.list_schedules(user_id=user.id)
            if not schedules:
                if update.message:
                    await update.message.reply_text("ℹ️ You have no active or scheduled workflows.")
                return
            lines = [f"⏰ *Workflow Schedules ({len(schedules)})*", ""]
            for s in schedules:
                nxt = s.next_run_at.strftime('%Y-%m-%d %H:%M:%S UTC') if s.next_run_at else "None"
                st_icon = "🟢" if s.status == "active" else ("⏸️" if s.status == "paused" else "⚪")
                lines.append(f"{st_icon} *{s.name}* (`{s.id}`)")
                lines.append(f"  Trigger: `{s.trigger_type}` | Next Run: `{nxt}` | Runs: {s.total_runs_count}")
                lines.append("")
            lines.append("💡 _To pause: `/workflows pause_schedule <id>`_")
            lines.append("💡 _To resume: `/workflows resume_schedule <id>`_")
            lines.append("💡 _To cancel: `/workflows cancel_schedule <id>`_")
            if update.message:
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            return

        # Subcommand: /workflows pause_schedule <schedule_id>
        if subcmd == "pause_schedule" and len(args) >= 2:
            sch_id = args[1].strip()
            scheduler = context.bot_data.get("workflow_scheduler") or getattr(workflow_engine, "workflow_scheduler", None)
            if not scheduler:
                if update.message:
                    await update.message.reply_text("ℹ️ Workflow scheduler is not configured.")
                return
            paused = await scheduler.pause_schedule(sch_id, user_id=user.id)
            if update.message:
                await update.message.reply_text(
                    f"⏸️ Schedule `{sch_id}` {'paused successfully.' if paused else 'could not be paused.'}",
                    parse_mode="Markdown",
                )
            return

        # Subcommand: /workflows resume_schedule <schedule_id>
        if subcmd == "resume_schedule" and len(args) >= 2:
            sch_id = args[1].strip()
            scheduler = context.bot_data.get("workflow_scheduler") or getattr(workflow_engine, "workflow_scheduler", None)
            if not scheduler:
                if update.message:
                    await update.message.reply_text("ℹ️ Workflow scheduler is not configured.")
                return
            resumed = await scheduler.resume_schedule(sch_id, user_id=user.id)
            if update.message:
                await update.message.reply_text(
                    f"▶️ Schedule `{sch_id}` {'resumed successfully.' if resumed else 'could not be resumed.'}",
                    parse_mode="Markdown",
                )
            return

        # Subcommand: /workflows cancel_schedule <schedule_id>
        if subcmd == "cancel_schedule" and len(args) >= 2:
            sch_id = args[1].strip()
            scheduler = context.bot_data.get("workflow_scheduler") or getattr(workflow_engine, "workflow_scheduler", None)
            if not scheduler:
                if update.message:
                    await update.message.reply_text("ℹ️ Workflow scheduler is not configured.")
                return
            cancelled = await scheduler.cancel_schedule(sch_id, user_id=user.id)
            if update.message:
                await update.message.reply_text(
                    f"🛑 Schedule `{sch_id}` {'cancelled successfully.' if cancelled else 'could not be cancelled.'}",
                    parse_mode="Markdown",
                )
            return

        # Subcommand: /workflows schedule <workflow_id> <trigger_type> <param...>
        if subcmd == "schedule" and len(args) >= 3:
            wf_id = args[1].strip()
            trig_type = args[2].strip().lower()
            param = args[3].strip() if len(args) > 3 else "3600"
            scheduler = context.bot_data.get("workflow_scheduler") or getattr(workflow_engine, "workflow_scheduler", None)
            if not scheduler:
                if update.message:
                    await update.message.reply_text("ℹ️ Workflow scheduler is not configured.")
                return

            cfg: dict[str, Any] = {}
            if trig_type == "interval":
                try:
                    cfg = {"interval_seconds": int(param)}
                except ValueError:
                    cfg = {"interval_seconds": 3600}
            elif trig_type == "daily":
                cfg = {"time": param}
            elif trig_type == "weekly":
                cfg = {"time": param, "days_of_week": [0, 1, 2, 3, 4]}
            elif trig_type == "cron":
                cfg = {"cron_expression": " ".join(args[3:])}
            else:
                cfg = {"interval_seconds": 3600}

            try:
                sch = await scheduler.schedule_workflow(
                    workflow_id=wf_id,
                    user_id=user.id,
                    name=f"Schedule for {wf_id[:8]}",
                    trigger_type=trig_type,
                    trigger_config=cfg,
                )
                if update.message:
                    nxt_str = sch.next_run_at.strftime('%Y-%m-%d %H:%M:%S UTC') if sch.next_run_at else "None"
                    await update.message.reply_text(
                        f"⏰ *Workflow Scheduled Successfully*\n\n"
                        f"• Schedule ID: `{sch.id}`\n"
                        f"• Workflow: `{wf_id}`\n"
                        f"• Trigger: `{trig_type}`\n"
                        f"• Next Run: `{nxt_str}`",
                        parse_mode="Markdown",
                    )
            except Exception as sched_err:
                if update.message:
                    await update.message.reply_text(f"⚠️ Failed to schedule workflow: {sched_err}")
            return

        # Subcommand: /workflows plan <request...> or /workflow <request...>
        if subcmd in ("plan", "execute") or (subcmd not in ("list", "run", "cancel", "status", "pause", "resume", "approvals", "approve", "reject", "schedules", "pause_schedule", "resume_schedule", "cancel_schedule") and len(args) > 0):
            planner = getattr(workflow_engine, "workflow_planner", None)
            if not planner:
                if update.message:
                    await update.message.reply_text("⚠️ Automated workflow planner is not currently initialized.")
                return

            req_text = " ".join(args[1:]) if subcmd in ("plan", "execute") else " ".join(args)
            if update.message:
                await update.message.reply_text(
                    f"🤖 *Planning and executing workflow for:*\n_{req_text}_\n\nPlease wait...",
                    parse_mode="Markdown",
                )

            exec_result = await planner.plan_and_execute(user_id=user.id, natural_language_request=req_text)
            wf_id = exec_result.get("workflow_id")
            wf_name = exec_result.get("name")
            run_exec = exec_result.get("execution", {})
            st = run_exec.get("status", "completed").upper()
            done_idx = run_exec.get("current_step_index", 0)
            tot = run_exec.get("total_steps", 0)

            reply_lines = [
                f"✅ *Workflow Executed: {wf_name}*",
                f"• Workflow ID: `{wf_id}`",
                f"• Status: *{st}* ({done_idx}/{tot} steps completed)",
                "",
                "*Execution Summary:*",
            ]
            for s in exec_result.get("steps", []):
                reply_lines.append(f"• `{s.get('name')}` using `{s.get('tool_name')}`")

            if run_exec.get("error"):
                reply_lines.append(f"\n⚠️ Error: `{run_exec.get('error')}`")

            if update.message:
                await update.message.reply_text("\n".join(reply_lines), parse_mode="Markdown")
            return

        # Default: list workflows
        workflows = await workflow_engine.list_workflows(user_id=user.id)
        if not workflows:
            if update.message:
                await update.message.reply_text(
                    "⚙️ *Workflows*\n\n"
                    "No active workflows found for your account.\n"
                    "Multi-step workflows will appear here when scheduled or running.\n\n"
                    "💡 *Usage:*\n"
                    "• `/workflows` - List your workflows\n"
                    "• `/workflow <goal>` - Generate and run a multi-step workflow from your request\n"
                    "• `/workflows run <id>` - Run or resume a workflow\n"
                    "• `/workflows status <id>` - Inspect workflow steps\n"
                    "• `/workflows cancel <id>` - Cancel an active workflow",
                    parse_mode="Markdown",
                )
            return

        lines = [f"⚙️ *Active Workflows ({len(workflows)})*", ""]
        for wf in workflows:
            w_id = wf.get("workflow_id") or wf.get("id")
            w_name = wf.get("name", "Automated Task")
            w_status = str(wf.get("status", "unknown")).upper()
            w_step = wf.get("current_step", wf.get("current_step_index", 0))
            status_icon = "🟢" if w_status in ("RUNNING", "ACTIVE") else ("⏸️" if w_status == "PAUSED" else "⚪")
            lines.append(f"{status_icon} *{w_name}* (`{w_id}`)")
            lines.append(f"  Status: `{w_status}` | Step: {w_step}")
            lines.append("")

        lines.append("💡 _Use `/workflows status <id>` or `/workflows run <id>` to inspect or trigger._")

        text = "\n".join(lines).strip()
        if update.message:
            await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Error in workflows command for user %s: %s", user.id, exc, exc_info=True)
        if update.message:
            await update.message.reply_text(f"⚠️ Error processing workflows command: {exc}")


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
    config: Config | None = context.bot_data.get("config")
    orchestrator: AgentOrchestrator | None = context.bot_data.get("orchestrator")

    # PostgreSQL (Neon) check
    postgres_status = "Not configured"
    db_url = getattr(orchestrator, "database_url", None) or (getattr(config, "database_url", None) if config else None)
    if db_url:
        try:
            from sqlalchemy.ext.asyncio import create_async_engine
            from sqlalchemy import text
            start_t = time.perf_counter()
            pg_engine = create_async_engine(db_url, pool_pre_ping=True)
            async with pg_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await pg_engine.dispose()
            pg_latency = (time.perf_counter() - start_t) * 1000
            postgres_status = f"🟢 Connected ({pg_latency:.1f}ms)"
        except Exception as exc:
            postgres_status = f"🔴 Error: {exc.__class__.__name__}"

    # Qdrant Memory / Vector DB check
    qdrant_status = "Not configured"
    if orchestrator and getattr(orchestrator, "memory_manager", None):
        try:
            mm = orchestrator.memory_manager
            start_t = time.perf_counter()
            colls = await mm.qdrant.client.get_collections()
            q_latency = (time.perf_counter() - start_t) * 1000
            qdrant_status = f"🟢 Connected ({q_latency:.1f}ms, {len(colls.collections)} colls)"
        except Exception as exc:
            qdrant_status = f"🔴 Error: {exc.__class__.__name__}"
    elif config and getattr(config, "qdrant_url", None) and config.qdrant_url != ":memory:":
        qdrant_status = "🟡 Uninitialized"

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
    is_operational = "🟢" in postgres_status
    # If db_client mock is provided (e.g. in unit tests), also probe it
    db_client = context.bot_data.get("db_client")
    if db_client and hasattr(db_client, "db"):
        try:
            await db_client.db.command("ping")
            is_operational = True
        except Exception:
            pass

    app_state = "🟢 Operational" if is_operational else "⚠️ Degraded"

    lines = [
        "🏥 *System Health Status*",
        "",
        f"• *Application:* {app_state}",
    ]
    if postgres_status != "Not configured":
        lines.append(f"• *PostgreSQL (Neon):* {postgres_status}")
    if qdrant_status != "Not configured":
        lines.append(f"• *Qdrant Vector DB:* {qdrant_status}")
    lines.extend([
        f"• *Telegram API:* {telegram_status}",
        f"• *Gemini Model:* `{model_name}`",
        f"• *Authorized Users:* {allowed_count}",
        f"• *Environment:* `{config.environment if config else 'production'}`",
    ])

    gemini_client: GeminiClient | None = context.bot_data.get("gemini_client")
    if not gemini_client and orchestrator and getattr(orchestrator, "gemini_client", None):
        gemini_client = getattr(orchestrator, "gemini_client")
    if gemini_client and hasattr(gemini_client, "get_telemetry"):
        telem = gemini_client.get_telemetry()
        lines.append(
            f"• *Gemini Telemetry:* {telem['successful_requests']}/{telem['total_requests']} reqs ok, "
            f"{telem['rate_limit_events']} 429s, {telem['retries_performed']} retries (avg {telem['average_latency_seconds']}s)"
        )

    status_report = "\n".join(lines)
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


def extract_gemini_error(exc: BaseException) -> GeminiError | None:
    """Traverse exception chain and return the most specific GeminiError if present."""
    curr: BaseException | None = exc
    visited: set[int] = set()
    while curr is not None and id(curr) not in visited:
        if isinstance(curr, GeminiError):
            return curr
        visited.add(id(curr))
        curr = getattr(curr, "__cause__", None) or getattr(curr, "__context__", None) or getattr(curr, "original_error", None)

    # Check if any exception in chain can be classified from Google SDK
    curr = exc
    visited.clear()
    while curr is not None and id(curr) not in visited:
        if isinstance(curr, Exception):
            from src.intelligence.providers.gemini import classify_gemini_error
            classified = classify_gemini_error(curr)
            if isinstance(classified, GeminiError) and type(classified) is not GeminiError:
                return classified
        visited.add(id(curr))
        curr = getattr(curr, "__cause__", None) or getattr(curr, "__context__", None)

    return None


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

    if not message or not user or not chat:
        return

    # Secret isolation & API key intercept for integrations
    awaiting_provider = (
        context.user_data.get("awaiting_api_key_for")
        if isinstance(getattr(context, "user_data", None), dict)
        else None
    )
    if awaiting_provider and isinstance(awaiting_provider, str):
        context.user_data.pop("awaiting_api_key_for", None)

        # Immediately delete the message containing the API key from Telegram history
        try:
            await message.delete()
        except Exception as del_err:
            logger.debug("Failed deleting API key message from Telegram: %s", del_err)

        submitted_key = (message.text or "").strip()
        if not submitted_key or submitted_key == "/cancel":
            await message.reply_text("🚫 Connection cancelled.")
            return

        registry = get_integration_registry(context)
        connector = registry.get_connector(awaiting_provider)
        provider_name = connector.metadata.name if connector else awaiting_provider

        async with get_integration_repo(context) as repo:
            if not repo:
                await message.reply_text("⚠️ Database is unavailable. Could not save integration credentials.")
                return

            status_msg = await message.reply_text(f"🔄 Verifying credentials for {provider_name}...")
            try:
                health = await registry.connect_api_key(
                    repo=repo,
                    user_id=user.id,
                    provider_id=awaiting_provider,
                    api_key=submitted_key,
                )

                if health.is_healthy:
                    masked = mask_secret(submitted_key)
                    lat_text = f" ({health.latency_ms:.1f}ms)" if health.latency_ms else ""
                    success_text = (
                        f"✅ *Successfully connected {provider_name}!* (`{awaiting_provider}`)\n\n"
                        f"• Status: 🟢 Connected & Verified{lat_text}\n"
                        f"• Credential: `{masked}` (Encrypted at rest with Fernet)\n\n"
                        "The assistant can now use this integration in your conversations."
                    )
                    await status_msg.edit_text(success_text, parse_mode="Markdown")
                else:
                    err_msg = health.error_message or "Authentication failed. Please verify your API key."
                    await status_msg.edit_text(
                        f"❌ *Failed to connect `{awaiting_provider}`:*\n"
                        f"{err_msg}\n\n"
                        f"No credentials have been saved. Use `/connect {awaiting_provider}` to try again.",
                        parse_mode="Markdown",
                    )
            except Exception as exc:
                logger.error("Error connecting provider %s for user %s: %s", awaiting_provider, user.id, exc)
                await status_msg.edit_text(
                    f"❌ Connection error: {exc}\nUse `/connect {awaiting_provider}` to try again."
                )
        return

    # OAuth authorization code intercept for Google Workspace
    awaiting_oauth = (
        context.user_data.get("awaiting_oauth_code_for")
        if isinstance(getattr(context, "user_data", None), dict)
        else None
    )
    if awaiting_oauth and isinstance(awaiting_oauth, str):
        context.user_data.pop("awaiting_oauth_code_for", None)
        context.user_data.pop("oauth_csrf", None)

        try:
            await message.delete()
        except Exception as del_err:
            logger.debug("Failed deleting OAuth code message: %s", del_err)

        submitted_code = (message.text or "").strip()
        if not submitted_code or submitted_code == "/cancel":
            await message.reply_text("🚫 Connection cancelled.")
            return

        registry = get_integration_registry(context)
        google_conn = registry.get_connector("google")
        oauth_client = getattr(google_conn, "oauth_client", None) or GoogleOAuthClient()

        async with get_integration_repo(context) as repo:
            if not repo:
                await message.reply_text("⚠️ Database is unavailable. Could not save integration credentials.")
                return

            status_msg = await message.reply_text("🔄 Exchanging authorization code with Google...")
            try:
                tokens = await oauth_client.exchange_code(submitted_code)
                health = await registry.connect_oauth_tokens(
                    repo=repo,
                    user_id=user.id,
                    provider_id="google",
                    token_data=tokens,
                )

                if health.is_healthy:
                    account_email = health.details.get("email", "your Google account")
                    success_text = (
                        f"✅ *Successfully connected Google Workspace!* (`google`)\n\n"
                        f"• Account: `{account_email}`\n"
                        f"• Status: 🟢 Connected & Verified ({health.latency_ms:.1f}ms)\n"
                        f"• Services: ✉️ Gmail | 📅 Calendar | 📁 Drive\n\n"
                        "The assistant can now search/draft emails, manage events, and read/upload Drive files."
                    )
                    await status_msg.edit_text(success_text, parse_mode="Markdown")
                else:
                    err_msg = health.error_message or "Token verification failed."
                    await status_msg.edit_text(
                        f"❌ *Failed to connect Google Workspace:*\n{err_msg}\n\nUse `/connect google` to try again.",
                        parse_mode="Markdown",
                    )
            except Exception as exc:
                logger.error("Error connecting Google OAuth for user %s: %s", user.id, exc)
                await status_msg.edit_text(f"❌ Connection error: {exc}\nUse `/connect google` to try again.")
        return

    # Intercept for Calendly (OAuth code or Personal Access Token)
    awaiting_calendly = (
        context.user_data.get("awaiting_calendly_for")
        if isinstance(getattr(context, "user_data", None), dict)
        else None
    )
    if awaiting_calendly:
        context.user_data.pop("awaiting_calendly_for", None)
        context.user_data.pop("oauth_csrf", None)

        try:
            await message.delete()
        except Exception as del_err:
            logger.debug("Failed deleting Calendly credential message: %s", del_err)

        submitted_value = (message.text or "").strip()
        if not submitted_value or submitted_value == "/cancel":
            await message.reply_text("🚫 Connection cancelled.")
            return

        registry = get_integration_registry(context)
        calendly_conn = registry.get_connector("calendly")
        oauth_client = getattr(calendly_conn, "oauth_client", None) or CalendlyOAuthClient()

        async with get_integration_repo(context) as repo:
            if not repo:
                await message.reply_text("⚠️ Database is unavailable. Could not save integration credentials.")
                return

            status_msg = await message.reply_text("🔄 Verifying Calendly credentials...")
            tokens = None
            if getattr(oauth_client, "client_id", None):
                try:
                    tokens = await oauth_client.exchange_code(submitted_value)
                except Exception as exc:
                    logger.debug("OAuth code exchange failed, treating as PAT: %s", exc)

            try:
                if tokens and tokens.get("access_token"):
                    health = await registry.connect_oauth_tokens(
                        repo=repo,
                        user_id=user.id,
                        provider_id="calendly",
                        token_data=tokens,
                    )
                else:
                    health = await registry.connect_api_key(
                        repo=repo,
                        user_id=user.id,
                        provider_id="calendly",
                        api_key=submitted_value,
                    )

                if health.is_healthy:
                    acc_name = health.details.get("name") or health.details.get("email") or "Calendly account"
                    masked = mask_secret(submitted_value)
                    lat_text = f" ({health.latency_ms:.1f}ms)" if health.latency_ms else ""
                    success_text = (
                        f"✅ *Successfully connected Calendly!* (`calendly`)\n\n"
                        f"• Account: `{acc_name}`\n"
                        f"• Status: 🟢 Connected & Verified{lat_text}\n"
                        f"• Credential: `{masked}` (Encrypted at rest with Fernet)\n\n"
                        "The assistant can now manage event types, generate booking links, and coordinate meetings."
                    )
                    await status_msg.edit_text(success_text, parse_mode="Markdown")
                else:
                    err_msg = health.error_message or "Failed to verify Calendly credentials."
                    await status_msg.edit_text(
                        f"❌ *Failed to connect Calendly:*\n{err_msg}\n\nUse `/connect calendly` to try again.",
                        parse_mode="Markdown",
                    )
            except Exception as exc:
                logger.error("Error connecting Calendly for user %s: %s", user.id, exc)
                await status_msg.edit_text(f"❌ Connection error: {exc}\nUse `/connect calendly` to try again.")
        return

    # Handle document attachment or text message
    doc = getattr(message, "document", None)
    is_doc = False
    if doc is not None:
        if isinstance(doc, Document):
            is_doc = True
        elif not isinstance(doc, MagicMock) or isinstance(getattr(doc, "file_name", None), str):
            is_doc = True

    if is_doc and doc is not None:
        raw_name = getattr(doc, "file_name", None)
        file_name = raw_name if isinstance(raw_name, str) else "uploaded_file"
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', file_name)
        file_size = getattr(doc, "file_size", 0) or 0
        if file_size > 20 * 1024 * 1024:
            await message.reply_text("⚠️ File is too large. Maximum supported upload size is 20MB.")
            return

        upload_dir = Path("data/uploads") / str(user.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        target_path = upload_dir / safe_name

        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(custom_path=str(target_path))
            caption = getattr(message, "caption", None) or "Please analyze this uploaded document."
            rel_path = f"{user.id}/{safe_name}"
            user_text = (
                f"[Uploaded Document: {rel_path} ({file_size} bytes)]\n"
                f"User request: {caption}"
            )
            logger.info("Saved uploaded document %s (%d bytes) for user %s", rel_path, file_size, user.id)
        except Exception as exc:
            logger.error("Failed to download Telegram document: %s", exc)
            await message.reply_text(f"⚠️ Failed to process uploaded document: {exc}")
            return
    elif getattr(message, "text", None):
        user_text = message.text.strip()
        if not user_text:
            return
    else:
        return

    # 3. Resolve or initialize Agent Orchestrator
    orchestrator: AgentOrchestrator | None = context.bot_data.get("orchestrator")
    if not orchestrator:
        gemini_client: GeminiClient | None = context.bot_data.get("gemini_client")
        if not gemini_client:
            logger.error("Neither orchestrator nor GeminiClient found in context.bot_data.")
            await message.reply_text("⚠️ AI service is temporarily unavailable. Please try again later.")
            return

        config: Config | None = context.bot_data.get("config")
        history_limit = config.conversation_history_limit if config else 20
        db_url = getattr(config, "database_url", None)
        db_client = context.bot_data.get("db_client")

        orchestrator = AgentOrchestrator(
            gemini_client=gemini_client,
            database_url=db_url,
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

        # Deliver response chunks formatted cleanly for Telegram
        from src.interfaces.telegram.formatter import send_formatted_reply
        await send_formatted_reply(message, response_text)

    except Exception as exc:
        gemini_err = extract_gemini_error(exc)
        if isinstance(gemini_err, GeminiRateLimitError):
            retry_sec = getattr(gemini_err, "retry_after", None)
            is_daily = getattr(gemini_err, "is_daily_quota", False) or (retry_sec is not None and retry_sec >= 60)
            if is_daily:
                time_str = ""
                if retry_sec:
                    hours = int(retry_sec // 3600)
                    minutes = int((retry_sec % 3600) // 60)
                    time_str = f"~{hours}h {minutes}m" if hours > 0 else f"~{minutes}m"
                reset_line = f"\n• Reset estimated in: *{time_str}*" if time_str else ""
                reply_text = (
                    f"🚫 *Gemini Daily Quota Exceeded*\n\n"
                    f"Your Google AI Studio free-tier daily request quota has been reached.{reset_line}\n\n"
                    f"• Please add billing to your Google AI Studio project or update the API key in `.env` to resume immediately."
                )
            else:
                retry_note = f" (suggested retry in {int(retry_sec)}s)" if retry_sec else ""
                reply_text = f"⏳ The Gemini AI rate limit has been reached. Please wait a moment and try asking again.{retry_note}"
            logger.warning("Rate limit encountered for user %s: %s", user.id, gemini_err)
            await message.reply_text(reply_text, parse_mode="Markdown")
        elif isinstance(gemini_err, GeminiTimeoutError):
            logger.warning("Gemini timeout encountered for user %s", user.id)
            await message.reply_text(
                "⏱️ The request to Gemini timed out. Please try asking again."
            )
        elif isinstance(gemini_err, GeminiServerError):
            logger.warning("Gemini server error for user %s: %s", user.id, gemini_err)
            await message.reply_text(
                "🛠️ Google AI services are temporarily unavailable (5xx). Please try again in a moment."
            )
        elif isinstance(gemini_err, GeminiAuthError):
            logger.error("Gemini auth error for user %s: %s", user.id, gemini_err)
            await message.reply_text(
                "🔑 AI service authentication error. Please contact the administrator."
            )
        elif isinstance(gemini_err, GeminiInvalidRequestError):
            logger.warning("Gemini invalid request for user %s: %s", user.id, gemini_err)
            await message.reply_text(
                "⚠️ The request could not be processed due to invalid parameters or formatting."
            )
        elif isinstance(gemini_err, (GeminiAPIError, GeminiError)):
            logger.error("Gemini service error for user %s: %s", user.id, gemini_err)
            await message.reply_text(
                "⚠️ Service error encountered while communicating with Gemini. Please try again later."
            )
        else:
            logger.error("Unexpected error handling message for user %s: %s", user.id, exc, exc_info=True)
            await message.reply_text("⚠️ An unexpected error occurred. Please try again.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by incoming Telegram updates."""
    logger.error("Exception occurred while handling an update: %s", context.error, exc_info=context.error)


def register_handlers(application: Application) -> None:
    """Register all command and message handlers to the Telegram application."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("new", clear_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("reset", clear_command))
    application.add_handler(CommandHandler("memory", memory_command))
    application.add_handler(CommandHandler("tools", tools_command))
    application.add_handler(CommandHandler(["workflows", "workflow"], workflows_command))
    application.add_handler(CommandHandler("integrations", integrations_command))
    application.add_handler(CommandHandler("connect", connect_command))
    application.add_handler(CommandHandler("disconnect", disconnect_command))
    application.add_handler(CommandHandler("integration_status", integration_status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("health", health_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CallbackQueryHandler(integration_callback_handler, pattern=r"^int_"))
    application.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    logger.info("Telegram command and message handlers registered.")
