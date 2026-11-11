"""Unified Google Workspace connector implementing BaseConnector."""

import logging
from typing import Any

from src.integrations.base import AuthType, BaseConnector, ConnectionHealth, ProviderMetadata
from src.integrations.connectors.google.oauth import GOOGLE_WORKSPACE_SCOPES, GoogleOAuthClient
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class GoogleWorkspaceConnector(BaseConnector):
    """Unified Google Workspace connector managing Gmail, Calendar, and Drive."""

    def __init__(
        self,
        oauth_client: GoogleOAuthClient | None = None,
        database_url: str | None = None,
    ) -> None:
        self.oauth_client = oauth_client or GoogleOAuthClient()
        self.database_url = database_url

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id="google",
            name="Google Workspace",
            description="Unified Google connection powering Gmail, Google Calendar, and Google Drive.",
            auth_type=AuthType.OAUTH2,
            required_scopes=GOOGLE_WORKSPACE_SCOPES,
            capabilities=[
                "gmail_read",
                "gmail_send",
                "calendar_read",
                "calendar_write",
                "drive_read",
                "drive_write",
            ],
            setup_guide="Authorize your Google Account via the link provided by /connect google",
            doc_url="https://developers.google.com/workspace",
        )

    async def test_connection(self, credentials: dict[str, Any]) -> ConnectionHealth:
        """Verify token validity against Google OAuth2 userinfo."""
        return await self.oauth_client.test_connection(credentials)

    async def refresh_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        """Refresh expired access token using refresh_token."""
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            return credentials
        refreshed = await self.oauth_client.refresh_access_token(refresh_token)
        credentials.update(refreshed)
        return credentials

    async def revoke_credentials(self, credentials: dict[str, Any]) -> bool:
        """Revoke Google OAuth token upon user disconnect."""
        token = credentials.get("refresh_token") or credentials.get("access_token")
        if token:
            return await self.oauth_client.revoke_token(token)
        return True

    async def get_tools(self, user_id: int, credentials: dict[str, Any]) -> list[BaseTool]:
        """Export all tools across Gmail, Calendar, and Drive."""
        from src.integrations.connectors.calendar.tools import get_calendar_tools
        from src.integrations.connectors.drive.tools import get_drive_tools
        from src.integrations.connectors.gmail.tools import get_gmail_tools

        access_token = credentials.get("access_token", "")
        tools: list[BaseTool] = []
        tools.extend(get_gmail_tools(access_token=access_token))
        tools.extend(get_calendar_tools(access_token=access_token))
        tools.extend(get_drive_tools(access_token=access_token))
        return tools

