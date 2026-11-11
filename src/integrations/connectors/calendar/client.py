"""Asynchronous REST client for the Google Calendar API v3."""

from datetime import datetime, timezone
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

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarClient:
    """Asynchronous client interacting with Google Calendar API v3."""

    def __init__(
        self,
        access_token: str,
        timeout: float = 15.0,
        http_client: IntegrationHttpClient | None = None,
    ) -> None:
        self.access_token = access_token.strip()
        self.timeout = timeout
        self.http_client = http_client or IntegrationHttpClient(
            provider_id="google_calendar",
            read_timeout=timeout,
        )

    def get_telemetry(self) -> dict[str, Any]:
        """Return production HTTP metrics and latency for Google Calendar."""
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
            logger.warning("Google Calendar authentication failure: %s", resp.text)
            raise AuthenticationError("Google Calendar authentication failed or token expired.", provider_id="google_calendar")
        if resp.status_code == 429:
            logger.warning("Google Calendar rate limit exceeded.")
            raise RateLimitError("Google Calendar quota or rate limit exceeded.", provider_id="google_calendar")
        if resp.status_code >= 500:
            logger.warning("Google Calendar server error: %s", resp.status_code)
            raise ProviderUnavailableError(f"Google Calendar service unavailable ({resp.status_code}).", provider_id="google_calendar")
        raise IntegrationError(f"Google Calendar error ({resp.status_code}): {resp.text[:200]}", provider_id="google_calendar")

    async def test_connection(self) -> ConnectionHealth:
        """Verify token validity by probing primary calendar."""
        if not self.access_token:
            return ConnectionHealth(status="unauthorized", is_healthy=False, error_message="Access token is empty.")

        start = time.perf_counter()
        try:
            resp = await self.http_client.get(f"{CALENDAR_API_BASE}/users/me/calendarList/primary", headers=self._headers, timeout=10.0)
            latency = round((time.perf_counter() - start) * 1000.0, 1)

            if resp.status_code == 200:
                data = resp.json()
                summary = data.get("summary", "Primary Calendar")
                time_zone = data.get("timeZone", "UTC")
                return ConnectionHealth(
                    status="healthy",
                    is_healthy=True,
                    latency_ms=latency,
                    details={"summary": summary, "timeZone": time_zone},
                )
            if resp.status_code in (401, 403):
                return ConnectionHealth(
                    status="unauthorized",
                    is_healthy=False,
                    latency_ms=latency,
                    error_message="Calendar access token invalid or expired.",
                )
            return ConnectionHealth(
                status="degraded",
                is_healthy=False,
                latency_ms=latency,
                error_message=f"Calendar check returned status {resp.status_code}.",
            )
        except Exception as exc:
            latency = round((time.perf_counter() - start) * 1000.0, 1)
            return ConnectionHealth(
                status="unhealthy",
                is_healthy=False,
                latency_ms=latency,
                error_message=f"Network error verifying Calendar: {exc}",
            )

    async def list_events(
        self,
        calendar_id: str = "primary",
        time_min: str | None = None,
        time_max: str | None = None,
        query: str | None = None,
        max_results: int = 15,
    ) -> list[dict[str, Any]]:
        """List events from calendar, sorted by start time."""
        params: dict[str, Any] = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": min(max(1, max_results), 50),
        }
        if time_min:
            params["timeMin"] = time_min
        else:
            params["timeMin"] = datetime.now(timezone.utc).isoformat()
        if time_max:
            params["timeMax"] = time_max
        if query:
            params["q"] = query.strip()

        resp = await self.http_client.get(f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events", headers=self._headers, params=params)
        if resp.status_code != 200:
            self._handle_response_error(resp)

        items = resp.json().get("items", [])
        results = []
        for item in items:
            start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
            end = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
            results.append({
                "id": item.get("id"),
                "summary": item.get("summary", "Untitled Event"),
                "start": start,
                "end": end,
                "description": item.get("description", ""),
                "location": item.get("location", ""),
                "status": item.get("status"),
                "html_link": item.get("htmlLink"),
                "attendees": [a.get("email") for a in item.get("attendees", []) if a.get("email")],
            })
        return results

    async def check_availability(
        self,
        time_min: str,
        time_max: str,
        calendar_id: str = "primary",
        timezone_str: str = "UTC",
    ) -> dict[str, Any]:
        """Check free/busy status for calendar within given time window."""
        payload = {
            "timeMin": time_min,
            "timeMax": time_max,
            "timeZone": timezone_str,
            "items": [{"id": calendar_id}],
        }
        resp = await self.http_client.post(f"{CALENDAR_API_BASE}/freeBusy", headers=self._headers, json=payload)
        if resp.status_code != 200:
            self._handle_response_error(resp)

        data = resp.json()
        busy_periods = data.get("calendars", {}).get(calendar_id, {}).get("busy", [])
        is_available = len(busy_periods) == 0

        return {
            "calendar_id": calendar_id,
            "time_min": time_min,
            "time_max": time_max,
            "is_available": is_available,
            "busy_periods": busy_periods,
            "busy_count": len(busy_periods),
        }

    async def create_event(
        self,
        summary: str,
        start_time: str,
        end_time: str,
        description: str = "",
        location: str = "",
        attendees: list[str] | None = None,
        timezone_str: str = "UTC",
        recurrence: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new calendar event."""
        event_body: dict[str, Any] = {
            "summary": summary.strip(),
            "description": description.strip(),
            "location": location.strip(),
            "start": {"dateTime": start_time, "timeZone": timezone_str},
            "end": {"dateTime": end_time, "timeZone": timezone_str},
        }
        if attendees:
            event_body["attendees"] = [{"email": a.strip()} for a in attendees if a.strip()]
        if recurrence:
            event_body["recurrence"] = recurrence

        resp = await self.http_client.post(f"{CALENDAR_API_BASE}/calendars/primary/events", headers=self._headers, json=event_body, idempotent=False)
        if resp.status_code not in (200, 201):
            self._handle_response_error(resp)

        data = resp.json()
        return {
            "id": data.get("id"),
            "summary": data.get("summary"),
            "start": data.get("start", {}).get("dateTime") or data.get("start", {}).get("date"),
            "end": data.get("end", {}).get("dateTime") or data.get("end", {}).get("date"),
            "status": "created",
            "html_link": data.get("htmlLink"),
        }

    async def update_event(
        self,
        event_id: str,
        summary: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
        timezone_str: str = "UTC",
    ) -> dict[str, Any]:
        """Update an existing calendar event."""
        # First retrieve current event
        current = await self.get_event(event_id)
        patch_body: dict[str, Any] = {}
        if summary is not None:
            patch_body["summary"] = summary
        if description is not None:
            patch_body["description"] = description
        if location is not None:
            patch_body["location"] = location
        if start_time is not None:
            patch_body["start"] = {"dateTime": start_time, "timeZone": timezone_str}
        if end_time is not None:
            patch_body["end"] = {"dateTime": end_time, "timeZone": timezone_str}
        if attendees is not None:
            patch_body["attendees"] = [{"email": a.strip()} for a in attendees if a.strip()]

        resp = await self.http_client.patch(
            f"{CALENDAR_API_BASE}/calendars/primary/events/{event_id}",
            headers=self._headers,
            json=patch_body,
        )
        if resp.status_code != 200:
            self._handle_response_error(resp)

        data = resp.json()
        return {
            "id": data.get("id"),
            "summary": data.get("summary"),
            "start": data.get("start", {}).get("dateTime") or data.get("start", {}).get("date"),
            "end": data.get("end", {}).get("dateTime") or data.get("end", {}).get("date"),
            "status": "updated",
            "html_link": data.get("htmlLink"),
        }

    async def delete_event(self, event_id: str, calendar_id: str = "primary") -> dict[str, Any]:
        """Delete an event by ID."""
        resp = await self.http_client.delete(f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events/{event_id}", headers=self._headers)
        if resp.status_code not in (200, 204):
            self._handle_response_error(resp)

        return {"id": event_id, "status": "deleted"}

    async def get_event(self, event_id: str, calendar_id: str = "primary") -> dict[str, Any]:
        """Retrieve details of a single event."""
        resp = await self.http_client.get(f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events/{event_id}", headers=self._headers)
        if resp.status_code != 200:
            self._handle_response_error(resp)

        item = resp.json()
        return {
            "id": item.get("id"),
            "summary": item.get("summary", "Untitled"),
            "description": item.get("description", ""),
            "location": item.get("location", ""),
            "start": item.get("start", {}).get("dateTime") or item.get("start", {}).get("date"),
            "end": item.get("end", {}).get("dateTime") or item.get("end", {}).get("date"),
            "status": item.get("status"),
            "attendees": [a.get("email") for a in item.get("attendees", []) if a.get("email")],
            "recurrence": item.get("recurrence", []),
            "html_link": item.get("htmlLink"),
        }

