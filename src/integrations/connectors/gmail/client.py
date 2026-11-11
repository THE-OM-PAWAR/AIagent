"""Asynchronous REST client for the Google Gmail API v1."""

import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
import time
from typing import Any

import httpx

from src.integrations.base import ConnectionHealth
from src.integrations.errors import (
    AuthenticationError,
    IntegrationError,
    ProviderUnavailableError,
    RateLimitError,
)
from src.integrations.http_client import IntegrationHttpClient

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _decode_base64url(data: str) -> str:
    """Safely decode base64url data from Gmail message payloads."""
    try:
        padded = data + "=" * (-len(data) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("utf-8"))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body_from_payload(payload: dict[str, Any]) -> str:
    """Extract readable text body from a nested Gmail message payload."""
    body_data = payload.get("body", {}).get("data")
    if body_data:
        return _decode_base64url(body_data)

    parts = payload.get("parts", [])
    text_content = []
    for part in parts:
        mime_type = part.get("mimeType", "")
        if mime_type == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                text_content.append(_decode_base64url(data))
        elif mime_type.startswith("multipart/"):
            nested = _extract_body_from_payload(part)
            if nested:
                text_content.append(nested)

    return "\n".join(text_content).strip()


class GmailClient:
    """Asynchronous client interacting with Gmail API v1 using OAuth2 Bearer token."""

    def __init__(
        self,
        access_token: str,
        timeout: float = 15.0,
        http_client: IntegrationHttpClient | None = None,
    ) -> None:
        self.access_token = access_token.strip()
        self.timeout = timeout
        self.http_client = http_client or IntegrationHttpClient(
            provider_id="gmail",
            read_timeout=timeout,
        )

    def get_telemetry(self) -> dict[str, Any]:
        """Return production HTTP metrics and latency for Gmail."""
        return self.http_client.get_telemetry()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _handle_response_error(self, resp: httpx.Response) -> None:
        """Translate HTTP status codes to typed IntegrationErrors."""
        if resp.status_code in (401, 403):
            logger.warning("Gmail API authentication failure: %s", resp.text)
            raise AuthenticationError("Gmail authentication failed or token expired.", provider_id="gmail")
        if resp.status_code == 429:
            logger.warning("Gmail API rate limit exceeded.")
            raise RateLimitError("Gmail API quota or rate limit exceeded.", provider_id="gmail")
        if resp.status_code >= 500:
            logger.warning("Gmail API server error: %s", resp.status_code)
            raise ProviderUnavailableError(f"Gmail service unavailable ({resp.status_code}).", provider_id="gmail")
        raise IntegrationError(f"Gmail API error ({resp.status_code}): {resp.text[:200]}", provider_id="gmail")

    async def test_connection(self) -> ConnectionHealth:
        """Verify token validity by probing Gmail user profile."""
        if not self.access_token:
            return ConnectionHealth(status="unauthorized", is_healthy=False, error_message="Access token is empty.")

        start = time.perf_counter()
        try:
            resp = await self.http_client.get(f"{GMAIL_API_BASE}/profile", headers=self._headers, timeout=10.0)
            latency = round((time.perf_counter() - start) * 1000.0, 1)

            if resp.status_code == 200:
                data = resp.json()
                email = data.get("emailAddress", "unknown")
                return ConnectionHealth(
                    status="healthy",
                    is_healthy=True,
                    latency_ms=latency,
                    details={"email": email, "messages_total": data.get("messagesTotal", 0)},
                )
            if resp.status_code in (401, 403):
                return ConnectionHealth(
                    status="unauthorized",
                    is_healthy=False,
                    latency_ms=latency,
                    error_message="Gmail access token invalid or expired.",
                )
            return ConnectionHealth(
                status="degraded",
                is_healthy=False,
                latency_ms=latency,
                error_message=f"Gmail profile returned status {resp.status_code}.",
            )
        except Exception as exc:
            latency = round((time.perf_counter() - start) * 1000.0, 1)
            return ConnectionHealth(
                status="unhealthy",
                is_healthy=False,
                latency_ms=latency,
                error_message=f"Network error verifying Gmail: {exc}",
            )

    async def search_messages(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """Search messages matching Gmail query string."""
        params = {"q": query.strip(), "maxResults": min(max(1, max_results), 50)}
        resp = await self.http_client.get(f"{GMAIL_API_BASE}/messages", headers=self._headers, params=params)
        if resp.status_code != 200:
            self._handle_response_error(resp)

        raw_msgs = resp.json().get("messages", [])
        results = []
        for m in raw_msgs[:max_results]:
            mid = m.get("id")
            if mid:
                meta = await self.get_message_metadata(mid)
                results.append(meta)
        return results

    async def get_message(self, message_id: str) -> dict[str, Any]:
        """Retrieve full message including decoded text body."""
        resp = await self.http_client.get(f"{GMAIL_API_BASE}/messages/{message_id}", headers=self._headers, params={"format": "full"})
        if resp.status_code != 200:
            self._handle_response_error(resp)

        data = resp.json()
        headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
        body = _extract_body_from_payload(data.get("payload", {}))

        return {
            "id": data.get("id"),
            "thread_id": data.get("threadId"),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "snippet": data.get("snippet", ""),
            "body": body,
            "label_ids": data.get("labelIds", []),
        }

    async def get_thread(self, thread_id: str) -> dict[str, Any]:
        """Retrieve all messages belonging to a conversation thread."""
        resp = await self.http_client.get(f"{GMAIL_API_BASE}/threads/{thread_id}", headers=self._headers, params={"format": "full"})
        if resp.status_code != 200:
            self._handle_response_error(resp)

        data = resp.json()
        raw_msgs = data.get("messages", [])
        messages = []
        for m in raw_msgs:
            headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
            body = _extract_body_from_payload(m.get("payload", {}))
            messages.append({
                "id": m.get("id"),
                "from": headers.get("from", ""),
                "to": headers.get("to", ""),
                "subject": headers.get("subject", ""),
                "date": headers.get("date", ""),
                "snippet": m.get("snippet", ""),
                "body": body,
            })

        return {
            "thread_id": data.get("id"),
            "history_id": data.get("historyId"),
            "messages_count": len(messages),
            "messages": messages,
        }

    async def get_message_metadata(self, message_id: str) -> dict[str, Any]:
        """Retrieve lightweight metadata for a message without full body payload."""
        params = {"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]}
        resp = await self.http_client.get(f"{GMAIL_API_BASE}/messages/{message_id}", headers=self._headers, params=params)
        if resp.status_code != 200:
            self._handle_response_error(resp)

        data = resp.json()
        headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
        return {
            "id": data.get("id"),
            "thread_id": data.get("threadId"),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "snippet": data.get("snippet", ""),
            "label_ids": data.get("labelIds", []),
        }

    def _create_mime_message(
        self,
        to: str,
        subject: str,
        body: str,
        is_html: bool = False,
        thread_id: str | None = None,
    ) -> str:
        """Create RFC 2822 base64url-encoded email payload."""
        message = MIMEMultipart("alternative")
        message["to"] = to.strip()
        message["subject"] = subject.strip()
        if thread_id:
            message["In-Reply-To"] = thread_id
            message["References"] = thread_id

        part = MIMEText(body, "html" if is_html else "plain", "utf-8")
        message.attach(part)
        raw_bytes = message.as_bytes()
        return base64.urlsafe_b64encode(raw_bytes).decode("utf-8")

    async def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        is_html: bool = False,
    ) -> dict[str, Any]:
        """Create a new message draft in the user's Gmail mailbox."""
        raw_b64 = self._create_mime_message(to, subject, body, is_html, thread_id)
        payload = {"message": {"raw": raw_b64}}
        if thread_id:
            payload["message"]["threadId"] = thread_id

        resp = await self.http_client.post(f"{GMAIL_API_BASE}/drafts", headers=self._headers, json=payload)
        if resp.status_code not in (200, 201):
            self._handle_response_error(resp)

        data = resp.json()
        return {
            "draft_id": data.get("id"),
            "message_id": data.get("message", {}).get("id"),
            "to": to,
            "subject": subject,
            "status": "draft_created",
        }

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        is_html: bool = False,
    ) -> dict[str, Any]:
        """Send an email immediately via Gmail API."""
        raw_b64 = self._create_mime_message(to, subject, body, is_html, thread_id)
        payload: dict[str, Any] = {"raw": raw_b64}
        if thread_id:
            payload["threadId"] = thread_id

        resp = await self.http_client.post(f"{GMAIL_API_BASE}/messages/send", headers=self._headers, json=payload, idempotent=False)
        if resp.status_code not in (200, 201):
            self._handle_response_error(resp)

        data = resp.json()
        return {
            "message_id": data.get("id"),
            "thread_id": data.get("threadId"),
            "to": to,
            "subject": subject,
            "status": "sent",
        }

    async def list_outreach_history(self, max_results: int = 15) -> list[dict[str, Any]]:
        """Track history of sent outreach emails."""
        return await self.search_messages(query="label:SENT", max_results=max_results)

