"""Calendly connector implementing BaseConnector."""

import logging
from typing import Any

from src.integrations.base import AuthType, BaseConnector, ConnectionHealth, ProviderMetadata
from src.integrations.connectors.calendly.client import CalendlyClient
from src.integrations.connectors.calendly.oauth import CalendlyOAuthClient
from src.integrations.connectors.calendly.tools import get_calendly_tools
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class CalendlyConnector(BaseConnector):
    """Connector for Calendly meeting scheduling platform."""

    def __init__(
        self,
        oauth_client: CalendlyOAuthClient | None = None,
        system_api_key: str | None = None,
        database_url: str | None = None,
    ) -> None:
        self.oauth_client = oauth_client or CalendlyOAuthClient()
        self.system_api_key = system_api_key
        self.database_url = database_url

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id="calendly",
            name="Calendly",
            description="Manage meeting types, generate scheduling links, track bookings, and retrieve invitee details.",
            auth_type=AuthType.OAUTH2,
            capabilities=[
                "get_user_info",
                "list_event_types",
                "create_scheduling_link",
                "list_scheduled_events",
                "get_invitees",
                "track_booking_updates",
            ],
            setup_guide="Connect via /connect calendly using an authorization link or Personal Access Token.",
            doc_url="https://developer.calendly.com",
        )

    def _resolve_token(self, credentials: dict[str, Any]) -> str:
        return credentials.get("access_token") or credentials.get("api_key") or self.system_api_key or ""

    async def test_connection(self, credentials: dict[str, Any]) -> ConnectionHealth:
        token = self._resolve_token(credentials)
        client = CalendlyClient(token=token)
        return await client.test_connection()

    async def refresh_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            return credentials
        refreshed = await self.oauth_client.refresh_access_token(refresh_token)
        credentials.update(refreshed)
        return credentials

    async def revoke_credentials(self, credentials: dict[str, Any]) -> bool:
        token = credentials.get("refresh_token") or credentials.get("access_token") or credentials.get("api_key")
        if token:
            return await self.oauth_client.revoke_token(token)
        return True

    async def get_tools(self, user_id: int, credentials: dict[str, Any]) -> list[BaseTool]:
        token = self._resolve_token(credentials)
        return get_calendly_tools(token=token, database_url=self.database_url)

