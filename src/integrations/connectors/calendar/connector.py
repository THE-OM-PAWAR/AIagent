"""Google Calendar standalone connector implementing BaseConnector."""

import logging
from typing import Any

from src.integrations.base import AuthType, BaseConnector, ConnectionHealth, ProviderMetadata
from src.integrations.connectors.calendar.client import GoogleCalendarClient
from src.integrations.connectors.calendar.tools import get_calendar_tools
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class GoogleCalendarConnector(BaseConnector):
    """Connector for Google Calendar service."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id="google_calendar",
            name="Google Calendar",
            description="Manage schedule, meetings, event planning, and check free/busy availability.",
            auth_type=AuthType.OAUTH2,
            required_scopes=["https://www.googleapis.com/auth/calendar.events"],
            capabilities=[
                "list_events",
                "check_availability",
                "create_events",
                "update_events",
                "delete_events",
            ],
            setup_guide="Connect via /connect google to link your Google Calendar.",
            doc_url="https://developers.google.com/calendar/api",
        )

    async def test_connection(self, credentials: dict[str, Any]) -> ConnectionHealth:
        token = credentials.get("access_token", "")
        client = GoogleCalendarClient(access_token=token)
        return await client.test_connection()

    async def get_tools(self, user_id: int, credentials: dict[str, Any]) -> list[BaseTool]:
        token = credentials.get("access_token", "")
        return get_calendar_tools(access_token=token, database_url=self.database_url)

