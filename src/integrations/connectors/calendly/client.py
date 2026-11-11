"""Asynchronous REST client for the Calendly API v2 using IntegrationHttpClient."""

import logging
from typing import Any

from src.integrations.base import ConnectionHealth
from src.integrations.errors import (
    AuthenticationError,
    IntegrationError,
    ProviderUnavailableError,
    RateLimitError,
)
from src.integrations.http_client import IntegrationHttpClient

logger = logging.getLogger(__name__)

CALENDLY_API_BASE = "https://api.calendly.com"


class CalendlyClient:
    """Asynchronous client interacting with Calendly API v2 using OAuth2 Bearer token or PAT."""

    def __init__(
        self,
        token: str,
        http_client: IntegrationHttpClient | None = None,
    ) -> None:
        self.token = token.strip()
        self.http_client = http_client or IntegrationHttpClient(provider_id="calendly")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def test_connection(self) -> ConnectionHealth:
        """Verify token validity by querying Calendly /users/me."""
        if not self.token:
            return ConnectionHealth(status="unauthorized", is_healthy=False, error_message="Calendly token is empty.")

        try:
            resp = await self.http_client.get(f"{CALENDLY_API_BASE}/users/me", headers=self._headers)
            if resp.status_code == 200:
                res = resp.json().get("resource", {})
                return ConnectionHealth(
                    status="healthy",
                    is_healthy=True,
                    details={
                        "email": res.get("email"),
                        "name": res.get("name"),
                        "scheduling_url": res.get("scheduling_url"),
                    },
                )
            return ConnectionHealth(status="degraded", is_healthy=False, error_message=f"HTTP {resp.status_code}")
        except AuthenticationError as auth_err:
            return ConnectionHealth(status="unauthorized", is_healthy=False, error_message=str(auth_err))
        except Exception as exc:
            return ConnectionHealth(status="unhealthy", is_healthy=False, error_message=f"Network error: {exc}")

    async def get_current_user(self) -> dict[str, Any]:
        """Retrieve the currently authenticated Calendly user profile."""
        resp = await self.http_client.get(f"{CALENDLY_API_BASE}/users/me", headers=self._headers)
        data = resp.json()
        res = data.get("resource", {})
        return {
            "uri": res.get("uri"),
            "name": res.get("name"),
            "email": res.get("email"),
            "scheduling_url": res.get("scheduling_url"),
            "timezone": res.get("timezone"),
            "organization_uri": res.get("current_organization"),
        }

    async def list_event_types(self, user_uri: str | None = None) -> list[dict[str, Any]]:
        """List active event types for the authenticated user."""
        target_user = user_uri
        if not target_user:
            profile = await self.get_current_user()
            target_user = profile.get("uri")

        params = {"user": target_user, "active": "true"}
        resp = await self.http_client.get(f"{CALENDLY_API_BASE}/event_types", headers=self._headers, params=params)
        data = resp.json()
        items = data.get("collection", [])

        results = []
        for item in items:
            results.append({
                "uri": item.get("uri"),
                "name": item.get("name"),
                "slug": item.get("slug"),
                "duration_minutes": item.get("duration"),
                "scheduling_url": item.get("scheduling_url"),
                "description": item.get("description_plain", ""),
                "color": item.get("color"),
                "kind": item.get("kind"),
            })
        return results

    async def get_scheduling_link(
        self,
        event_type_uri: str,
        max_event_count: int = 1,
    ) -> dict[str, Any]:
        """Create a single-use booking link for an event type."""
        payload = {
            "max_event_count": max_event_count,
            "owner": event_type_uri.strip(),
            "owner_type": "EventType",
        }
        resp = await self.http_client.post(
            f"{CALENDLY_API_BASE}/scheduling_links",
            headers=self._headers,
            json=payload,
            idempotent=False,
        )
        data = resp.json()
        res = data.get("resource", {})
        return {
            "booking_url": res.get("booking_url"),
            "owner": res.get("owner"),
            "owner_type": res.get("owner_type"),
        }

    async def list_scheduled_events(
        self,
        status: str = "active",
        user_uri: str | None = None,
        min_start_time: str | None = None,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """List scheduled bookings filtered by status and start time."""
        target_user = user_uri
        if not target_user:
            profile = await self.get_current_user()
            target_user = profile.get("uri")

        params: dict[str, Any] = {
            "user": target_user,
            "status": status,
            "count": min(max(1, max_results), 100),
        }
        if min_start_time:
            params["min_start_time"] = min_start_time

        resp = await self.http_client.get(f"{CALENDLY_API_BASE}/scheduled_events", headers=self._headers, params=params)
        data = resp.json()
        items = data.get("collection", [])

        results = []
        for item in items:
            uri = item.get("uri", "")
            event_uuid = uri.rstrip("/").split("/")[-1] if uri else ""
            results.append({
                "uri": uri,
                "event_uuid": event_uuid,
                "name": item.get("name"),
                "status": item.get("status"),
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "location": item.get("location", {}),
                "event_type_uri": item.get("event_type"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            })
        return results

    async def get_invitees(self, event_uuid_or_uri: str) -> list[dict[str, Any]]:
        """Retrieve invitee information for a scheduled event."""
        event_uuid = event_uuid_or_uri.rstrip("/").split("/")[-1]
        endpoint = f"{CALENDLY_API_BASE}/scheduled_events/{event_uuid}/invitees"

        resp = await self.http_client.get(endpoint, headers=self._headers)
        data = resp.json()
        items = data.get("collection", [])

        invitees = []
        for inv in items:
            invitees.append({
                "uri": inv.get("uri"),
                "name": inv.get("name"),
                "email": inv.get("email"),
                "status": inv.get("status"),
                "timezone": inv.get("timezone"),
                "text_reminder_number": inv.get("text_reminder_number"),
                "questions_and_answers": inv.get("questions_and_answers", []),
                "cancellation_reason": inv.get("cancellation", {}).get("reason") if inv.get("cancellation") else None,
                "canceled_by": inv.get("cancellation", {}).get("canceled_by") if inv.get("cancellation") else None,
            })
        return invitees

    async def list_booking_updates(
        self,
        min_start_time: str | None = None,
        max_results: int = 15,
    ) -> list[dict[str, Any]]:
        """Retrieve recent bookings, updates, and cancellations."""
        active = await self.list_scheduled_events(status="active", min_start_time=min_start_time, max_results=max_results)
        canceled = await self.list_scheduled_events(status="canceled", min_start_time=min_start_time, max_results=max_results)

        combined = active + canceled
        combined.sort(key=lambda x: x.get("updated_at") or x.get("start_time") or "", reverse=True)
        return combined[:max_results]

