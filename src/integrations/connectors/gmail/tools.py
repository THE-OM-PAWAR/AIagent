"""Gmail tool implementations exposing BaseTool interfaces."""

import hashlib
import logging
from typing import Any

from src.integrations.connectors.gmail.client import GmailClient
from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)


async def _resolve_client(
    client: GmailClient | None,
    user_id: int | None,
    integration_registry: Any | None,
    database_url: str | None,
) -> GmailClient | None:
    """Resolve active GmailClient dynamically using user credentials if available."""
    if user_id and integration_registry and database_url:
        try:
            from src.database.repositories.integration_repo import IntegrationRepository
            from src.database.session import get_async_session

            async with get_async_session(database_url) as session:
                repo = IntegrationRepository(session)
                # Check gmail or google workspace credentials
                creds = await integration_registry.get_credentials(repo, user_id, "gmail")
                if not creds:
                    creds = await integration_registry.get_credentials(repo, user_id, "google")

                if creds and creds.get("access_token"):
                    return GmailClient(access_token=creds["access_token"])
        except Exception as exc:
            logger.debug("Failed resolving user Gmail credentials: %s", exc)

    return client


class GmailSearchTool(BaseTool):
    """Search messages in the user's Gmail inbox."""

    def __init__(
        self,
        client: GmailClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "gmail_search_emails"

    @property
    def description(self) -> str:
        return (
            "Search for emails in the user's Gmail mailbox using queries such as sender, "
            "subject, keywords, or labels (e.g., 'from:john', 'subject:invoice', 'is:unread')."
        )

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Standard Gmail search query string (e.g., 'from:example.com is:unread').",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of messages to return (default: 5, max: 20).",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, max_results: int = 5, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Gmail is not connected. Use /connect google in Telegram to connect your account."}

        messages = await active_client.search_messages(query=query, max_results=max_results)
        return {
            "query": query,
            "results_count": len(messages),
            "messages": messages,
        }


class GmailReadTool(BaseTool):
    """Read a specific message or conversation thread from Gmail."""

    def __init__(
        self,
        client: GmailClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "gmail_read_message"

    @property
    def description(self) -> str:
        return "Read the full text content, sender, subject, and thread history for a given Gmail message_id or thread_id."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The unique Gmail message ID to read.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Optional thread ID to retrieve all messages in the conversation thread.",
                },
            },
        }

    async def execute(
        self,
        message_id: str | None = None,
        thread_id: str | None = None,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Gmail is not connected. Use /connect google in Telegram to connect your account."}

        if thread_id:
            return await active_client.get_thread(thread_id)
        if message_id:
            return await active_client.get_message(message_id)
        return {"error": "Either message_id or thread_id must be provided."}


class GmailMetadataTool(BaseTool):
    """Retrieve message headers and metadata without downloading full email body."""

    def __init__(
        self,
        client: GmailClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "gmail_get_message_metadata"

    @property
    def description(self) -> str:
        return "Retrieve lightweight message metadata (From, To, Subject, Date, Labels) for a Gmail message ID."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Unique Gmail message ID."},
            },
            "required": ["message_id"],
        }

    async def execute(self, message_id: str, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Gmail is not connected. Use /connect google in Telegram to connect your account."}

        return await active_client.get_message_metadata(message_id=message_id)


class GmailDraftTool(BaseTool):
    """Create an email draft in Gmail."""

    def __init__(
        self,
        client: GmailClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "gmail_create_draft"

    @property
    def description(self) -> str:
        return "Create a new email draft in the user's Gmail account without sending it."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string", "description": "Subject of the email."},
                "body": {"type": "string", "description": "Body text of the email."},
                "thread_id": {"type": "string", "description": "Optional thread ID to reply to."},
            },
            "required": ["to", "subject", "body"],
        }

    async def execute(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Gmail is not connected. Use /connect google in Telegram to connect your account."}

        return await active_client.create_draft(to=to, subject=subject, body=body, thread_id=thread_id)


# Idempotency cache of recently sent emails to prevent duplicate transmissions
_SENT_EMAIL_CACHE: dict[str, dict[str, Any]] = {}


def _get_send_idempotency_key(user_id: int | None, to: str, subject: str, body: str, thread_id: str | None) -> str:
    """Generate a stable deterministic signature for an email transmission."""
    normalized = f"{user_id or 0}:{to.strip().lower()}:{subject.strip()}:{body.strip()}:{thread_id or ''}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class GmailSendTool(BaseTool):
    """Send an email via Gmail with mandatory explicit human-in-the-loop approval and idempotency."""

    def __init__(
        self,
        client: GmailClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "gmail_send_email"

    @property
    def description(self) -> str:
        return (
            "Send an email via the user's Gmail account. CRITICAL: Sending an email requires explicit "
            "user confirmation (confirmed=True) and sensitive authorization. If confirmation has not been "
            "provided by the user, first show the draft to the user and request their explicit approval, "
            "or use 'gmail_create_draft' to safely stage the email without sending."
        )

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.SENSITIVE

    @property
    def requires_approval(self) -> bool:
        return True

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string", "description": "Subject of the email."},
                "body": {"type": "string", "description": "Full body text of the email."},
                "thread_id": {"type": "string", "description": "Optional thread ID to reply to."},
                "confirmed": {
                    "type": "boolean",
                    "description": "Must be True ONLY when the human user has explicitly instructed to send this email.",
                    "default": False,
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional client-supplied unique key to prevent duplicate email transmissions.",
                },
            },
            "required": ["to", "subject", "body"],
        }

    async def execute(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        confirmed: bool = False,
        idempotency_key: str | None = None,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Gmail is not connected. Use /connect google in Telegram to connect your account."}

        # Enforce explicit user approval guard
        if not confirmed:
            return {
                "status": "pending_approval",
                "requires_approval": True,
                "tool_name": "gmail_send_email",
                "message": (
                    f"⚠️ Sending email to '{to}' requires explicit user confirmation. "
                    "The email has NOT been sent. Please present this draft to the user and ask them to confirm before proceeding. "
                    "Alternatively, you may call 'gmail_create_draft' to save this safely into drafts."
                ),
                "draft_details": {
                    "to": to,
                    "subject": subject,
                    "body_preview": body[:300] + ("..." if len(body) > 300 else ""),
                },
                "safe_alternative": "gmail_create_draft",
            }

        # Enforce idempotency: prevent duplicate send on retries or identical calls
        key = idempotency_key or _get_send_idempotency_key(user_id, to, subject, body, thread_id)
        if key in _SENT_EMAIL_CACHE:
            cached = _SENT_EMAIL_CACHE[key]
            logger.warning(
                "GmailSendTool: duplicate send prevented by idempotency guard for key=%s to=%s (existing message_id=%s)",
                key[:12],
                to,
                cached.get("message_id"),
            )
            return {
                "status": "sent",
                "already_sent": True,
                "message_id": cached.get("message_id"),
                "thread_id": cached.get("thread_id"),
                "to": to,
                "subject": subject,
                "note": "Duplicate email send prevented by idempotency guard; email was previously sent successfully.",
            }

        result = await active_client.send_email(to=to, subject=subject, body=body, thread_id=thread_id)

        # Outcome verification: confirm message was accepted with a valid message_id
        msg_id = result.get("message_id")
        if not msg_id:
            logger.error("GmailSendTool: send operation failed outcome verification: missing message_id from API")
            return {
                "status": "error",
                "error": "Gmail API send outcome verification failed: no valid message ID returned.",
            }

        # Store in idempotency cache
        _SENT_EMAIL_CACHE[key] = {
            "message_id": msg_id,
            "thread_id": result.get("thread_id"),
            "to": to,
            "subject": subject,
            "sent_at": result.get("sent_at"),
        }
        if len(_SENT_EMAIL_CACHE) > 500:
            _SENT_EMAIL_CACHE.pop(next(iter(_SENT_EMAIL_CACHE)))

        logger.info("Gmail: email sent to '%s' (message_id=%s, user_id=%s)", to, msg_id, user_id)
        return result


class GmailOutreachHistoryTool(BaseTool):
    """Track and list outreach history from sent emails."""

    def __init__(
        self,
        client: GmailClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "gmail_list_outreach_history"

    @property
    def description(self) -> str:
        return "Track and retrieve the user's recent email outreach history from the Sent folder."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of sent outreach emails to return (default: 10).",
                    "default": 10,
                },
            },
        }

    async def execute(self, max_results: int = 10, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Gmail is not connected. Use /connect google in Telegram to connect your account."}

        outreach = await active_client.list_outreach_history(max_results=max_results)
        return {
            "outreach_count": len(outreach),
            "outreach_history": outreach,
        }


def get_gmail_tools(
    access_token: str | None = None,
    client: GmailClient | None = None,
    integration_registry: Any | None = None,
    database_url: str | None = None,
) -> list[BaseTool]:
    """Factory creating all Gmail tools configured with credentials."""
    gmail_client = client or (GmailClient(access_token=access_token) if access_token else None)
    return [
        GmailSearchTool(client=gmail_client, integration_registry=integration_registry, database_url=database_url),
        GmailReadTool(client=gmail_client, integration_registry=integration_registry, database_url=database_url),
        GmailMetadataTool(client=gmail_client, integration_registry=integration_registry, database_url=database_url),
        GmailDraftTool(client=gmail_client, integration_registry=integration_registry, database_url=database_url),
        GmailSendTool(client=gmail_client, integration_registry=integration_registry, database_url=database_url),
        GmailOutreachHistoryTool(client=gmail_client, integration_registry=integration_registry, database_url=database_url),
    ]
