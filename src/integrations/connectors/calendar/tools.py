"""Google Calendar tool implementations exposing BaseTool interfaces."""

import logging
from typing import Any

from src.integrations.connectors.calendar.client import GoogleCalendarClient
from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)


async def _resolve_client(
    client: GoogleCalendarClient | None,
    user_id: int | None,
    integration_registry: Any | None,
    database_url: str | None,
) -> GoogleCalendarClient | None:
    """Resolve active GoogleCalendarClient dynamically using user credentials if available."""
    if user_id and integration_registry and database_url:
        try:
            from src.database.repositories.integration_repo import IntegrationRepository
            from src.database.session import get_async_session

            async with get_async_session(database_url) as session:
                repo = IntegrationRepository(session)
                creds = await integration_registry.get_credentials(repo, user_id, "google_calendar")
                if not creds:
                    creds = await integration_registry.get_credentials(repo, user_id, "google")

                if creds and creds.get("access_token"):
                    return GoogleCalendarClient(access_token=creds["access_token"])
        except Exception as exc:
            logger.debug("Failed resolving user Calendar credentials: %s", exc)

    return client


class CalendarListEventsTool(BaseTool):
    """List events from Google Calendar."""

    def __init__(
        self,
        client: GoogleCalendarClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendar_list_events"

    @property
    def description(self) -> str:
        return "List scheduled upcoming events from the user's Google Calendar, optionally filtered by date range or keyword query."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "ISO-8601 start datetime string (e.g., '2026-03-01T09:00:00Z'). Defaults to now."},
                "time_max": {"type": "string", "description": "ISO-8601 end datetime string filter."},
                "query": {"type": "string", "description": "Free-text search terms to filter event summaries."},
                "max_results": {"type": "integer", "description": "Maximum number of events to return (default: 10).", "default": 10},
            },
        }

    async def execute(
        self,
        time_min: str | None = None,
        time_max: str | None = None,
        query: str | None = None,
        max_results: int = 10,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Google Calendar is not connected. Use /connect google in Telegram to connect."}

        events = await active_client.list_events(time_min=time_min, time_max=time_max, query=query, max_results=max_results)
        return {"events_count": len(events), "events": events}


class CalendarCheckAvailabilityTool(BaseTool):
    """Check schedule availability and busy slots."""

    def __init__(
        self,
        client: GoogleCalendarClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendar_check_availability"

    @property
    def description(self) -> str:
        return "Check if the user is free or busy during a specific time range on Google Calendar."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "start_time": {"type": "string", "description": "ISO-8601 start datetime string (e.g. '2026-03-01T14:00:00Z')."},
                "end_time": {"type": "string", "description": "ISO-8601 end datetime string (e.g. '2026-03-01T15:00:00Z')."},
                "timezone": {"type": "string", "description": "IANA timezone identifier (default: 'UTC').", "default": "UTC"},
            },
            "required": ["start_time", "end_time"],
        }

    async def execute(
        self,
        start_time: str,
        end_time: str,
        timezone: str = "UTC",
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Google Calendar is not connected. Use /connect google in Telegram to connect."}

        return await active_client.check_availability(time_min=start_time, time_max=end_time, timezone_str=timezone)


class CalendarCreateEventTool(BaseTool):
    """Create a new event on Google Calendar."""

    def __init__(
        self,
        client: GoogleCalendarClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendar_create_event"

    @property
    def description(self) -> str:
        return "Create a new meeting or event on Google Calendar with start/end datetimes, description, and attendees."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Title or headline of the event."},
                "start_time": {"type": "string", "description": "ISO-8601 start datetime (e.g. '2026-03-01T10:00:00Z')."},
                "end_time": {"type": "string", "description": "ISO-8601 end datetime (e.g. '2026-03-01T11:00:00Z')."},
                "description": {"type": "string", "description": "Optional notes or agenda for the meeting."},
                "location": {"type": "string", "description": "Optional location or video call link."},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of attendee email addresses.",
                },
                "timezone": {"type": "string", "description": "IANA timezone identifier (default: 'UTC').", "default": "UTC"},
            },
            "required": ["summary", "start_time", "end_time"],
        }

    async def execute(
        self,
        summary: str,
        start_time: str,
        end_time: str,
        description: str = "",
        location: str = "",
        attendees: list[str] | None = None,
        timezone: str = "UTC",
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Google Calendar is not connected. Use /connect google in Telegram to connect."}

        return await active_client.create_event(
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            description=description,
            location=location,
            attendees=attendees,
            timezone_str=timezone,
        )


class CalendarUpdateEventTool(BaseTool):
    """Update an existing event on Google Calendar."""

    def __init__(
        self,
        client: GoogleCalendarClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendar_update_event"

    @property
    def description(self) -> str:
        return "Update an existing calendar event's title, time, description, location, or attendees."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Unique Google Calendar event ID to modify."},
                "summary": {"type": "string", "description": "Updated event title."},
                "start_time": {"type": "string", "description": "Updated ISO-8601 start datetime."},
                "end_time": {"type": "string", "description": "Updated ISO-8601 end datetime."},
                "description": {"type": "string", "description": "Updated notes or agenda."},
                "location": {"type": "string", "description": "Updated location."},
                "timezone": {"type": "string", "description": "IANA timezone identifier.", "default": "UTC"},
            },
            "required": ["event_id"],
        }

    async def execute(
        self,
        event_id: str,
        summary: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        description: str | None = None,
        location: str | None = None,
        timezone: str = "UTC",
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Google Calendar is not connected. Use /connect google in Telegram to connect."}

        return await active_client.update_event(
            event_id=event_id,
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            description=description,
            location=location,
            timezone_str=timezone,
        )


class CalendarDeleteEventTool(BaseTool):
    """Delete an event from Google Calendar with mandatory confirmation."""

    def __init__(
        self,
        client: GoogleCalendarClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendar_delete_event"

    @property
    def description(self) -> str:
        return (
            "Permanently delete an event from Google Calendar. WARNING: Destructive operation. "
            "Requires explicit user confirmation (confirmed=True)."
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
                "event_id": {"type": "string", "description": "Unique Google Calendar event ID to delete."},
                "confirmed": {
                    "type": "boolean",
                    "description": "Must be True ONLY when user explicitly confirmed deletion.",
                    "default": False,
                },
            },
            "required": ["event_id"],
        }

    async def execute(
        self,
        event_id: str,
        confirmed: bool = False,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Google Calendar is not connected. Use /connect google in Telegram to connect."}

        if not confirmed:
            try:
                event_info = await active_client.get_event(event_id)
                summary = event_info.get("summary", event_id)
            except Exception:
                summary = event_id

            return {
                "status": "pending_approval",
                "requires_approval": True,
                "message": f"⚠️ Deleting event '{summary}' requires explicit confirmation. Please ask the user to confirm.",
                "event_id": event_id,
            }

        return await active_client.delete_event(event_id=event_id)


class CalendarGetEventTool(BaseTool):
    """Retrieve full details of a specific Google Calendar event."""

    def __init__(
        self,
        client: GoogleCalendarClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendar_get_event"

    @property
    def description(self) -> str:
        return "Get details of a specific calendar event by its event_id."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Unique Google Calendar event ID."},
            },
            "required": ["event_id"],
        }

    async def execute(self, event_id: str, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Google Calendar is not connected. Use /connect google in Telegram to connect."}

        return await active_client.get_event(event_id=event_id)


def get_calendar_tools(
    access_token: str | None = None,
    client: GoogleCalendarClient | None = None,
    integration_registry: Any | None = None,
    database_url: str | None = None,
) -> list[BaseTool]:
    """Factory creating all Google Calendar tools configured with credentials."""
    cal_client = client or (GoogleCalendarClient(access_token=access_token) if access_token else None)
    return [
        CalendarListEventsTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
        CalendarCheckAvailabilityTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
        CalendarCreateEventTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
        CalendarUpdateEventTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
        CalendarDeleteEventTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
        CalendarGetEventTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
    ]

