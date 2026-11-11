"""Calendly tool implementations exposing BaseTool interfaces."""

import logging
from typing import Any

from src.integrations.connectors.calendly.client import CalendlyClient
from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)


async def _resolve_client(
    client: CalendlyClient | None,
    user_id: int | None,
    integration_registry: Any | None,
    database_url: str | None,
) -> CalendlyClient | None:
    """Resolve active CalendlyClient dynamically using user credentials if available."""
    if user_id and integration_registry and database_url:
        try:
            from src.database.repositories.integration_repo import IntegrationRepository
            from src.database.session import get_async_session

            async with get_async_session(database_url) as session:
                repo = IntegrationRepository(session)
                creds = await integration_registry.get_credentials(repo, user_id, "calendly")
                if creds:
                    token = creds.get("access_token") or creds.get("api_key")
                    if token:
                        return CalendlyClient(token=token)
        except Exception as exc:
            logger.debug("Failed resolving user Calendly credentials: %s", exc)

    return client


class CalendlyGetCurrentUserTool(BaseTool):
    """Retrieve the authenticated Calendly user profile."""

    def __init__(
        self,
        client: CalendlyClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendly_get_current_user"

    @property
    def description(self) -> str:
        return "Retrieve the authenticated user's Calendly profile, scheduling URL, timezone, and organization URI."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.token:
            return {"error": "Calendly is not connected. Use /connect calendly in Telegram to connect."}

        return await active_client.get_current_user()


class CalendlyListEventTypesTool(BaseTool):
    """List active event types and booking page links."""

    def __init__(
        self,
        client: CalendlyClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendly_list_event_types"

    @property
    def description(self) -> str:
        return "List all active Calendly meeting types with durations, slugs, and public booking URLs."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_uri": {"type": "string", "description": "Optional Calendly user URI filter."},
            },
        }

    async def execute(self, user_uri: str | None = None, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.token:
            return {"error": "Calendly is not connected. Use /connect calendly in Telegram to connect."}

        event_types = await active_client.list_event_types(user_uri=user_uri)
        return {"event_types_count": len(event_types), "event_types": event_types}


class CalendlyGetSchedulingLinkTool(BaseTool):
    """Generate a single-use booking link for a meeting type."""

    def __init__(
        self,
        client: CalendlyClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendly_get_scheduling_link"

    @property
    def description(self) -> str:
        return "Generate a unique single-use booking link for a specific Calendly event type URI to send to a prospect."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "event_type_uri": {"type": "string", "description": "The URI of the event type (from calendly_list_event_types)."},
                "max_event_count": {"type": "integer", "description": "Number of times this link can be booked (default: 1).", "default": 1},
            },
            "required": ["event_type_uri"],
        }

    async def execute(
        self,
        event_type_uri: str,
        max_event_count: int = 1,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.token:
            return {"error": "Calendly is not connected. Use /connect calendly in Telegram to connect."}

        return await active_client.get_scheduling_link(event_type_uri=event_type_uri, max_event_count=max_event_count)


class CalendlyListScheduledEventsTool(BaseTool):
    """List scheduled meetings and bookings."""

    def __init__(
        self,
        client: CalendlyClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendly_list_scheduled_events"

    @property
    def description(self) -> str:
        return "List scheduled bookings from Calendly, filtered by active/canceled status and start datetime."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["active", "canceled"], "default": "active"},
                "min_start_time": {"type": "string", "description": "ISO-8601 start datetime filter (e.g. '2026-03-01T00:00:00Z')."},
                "max_results": {"type": "integer", "description": "Maximum number of bookings to return (default: 20).", "default": 20},
            },
        }

    async def execute(
        self,
        status: str = "active",
        min_start_time: str | None = None,
        max_results: int = 20,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.token:
            return {"error": "Calendly is not connected. Use /connect calendly in Telegram to connect."}

        events = await active_client.list_scheduled_events(status=status, min_start_time=min_start_time, max_results=max_results)
        return {"events_count": len(events), "events": events}


class CalendlyGetInviteesTool(BaseTool):
    """Retrieve attendee/invitee details for a scheduled meeting."""

    def __init__(
        self,
        client: CalendlyClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendly_get_invitees"

    @property
    def description(self) -> str:
        return "Retrieve attendee information (name, email, answers, cancellation reason) for a scheduled Calendly event."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "event_uuid_or_uri": {"type": "string", "description": "The scheduled event UUID or full URI."},
            },
            "required": ["event_uuid_or_uri"],
        }

    async def execute(self, event_uuid_or_uri: str, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.token:
            return {"error": "Calendly is not connected. Use /connect calendly in Telegram to connect."}

        invitees = await active_client.get_invitees(event_uuid_or_uri=event_uuid_or_uri)
        return {"invitees_count": len(invitees), "invitees": invitees}


class CalendlyBookingUpdatesTool(BaseTool):
    """Track recent booking creations and cancellations."""

    def __init__(
        self,
        client: CalendlyClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "calendly_list_booking_updates"

    @property
    def description(self) -> str:
        return "Track and retrieve recent booking activity, new reservations, and cancellations from Calendly."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "description": "Maximum updates to return (default: 15).", "default": 15},
                "min_start_time": {"type": "string", "description": "Optional ISO-8601 start time filter."},
            },
        }

    async def execute(
        self,
        max_results: int = 15,
        min_start_time: str | None = None,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.token:
            return {"error": "Calendly is not connected. Use /connect calendly in Telegram to connect."}

        updates = await active_client.list_booking_updates(min_start_time=min_start_time, max_results=max_results)
        return {"updates_count": len(updates), "updates": updates}


def get_calendly_tools(
    token: str | None = None,
    client: CalendlyClient | None = None,
    integration_registry: Any | None = None,
    database_url: str | None = None,
) -> list[BaseTool]:
    """Factory creating all Calendly tools configured with credentials."""
    cal_client = client or (CalendlyClient(token=token) if token else None)
    return [
        CalendlyGetCurrentUserTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
        CalendlyListEventTypesTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
        CalendlyGetSchedulingLinkTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
        CalendlyListScheduledEventsTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
        CalendlyGetInviteesTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
        CalendlyBookingUpdatesTool(client=cal_client, integration_registry=integration_registry, database_url=database_url),
    ]

