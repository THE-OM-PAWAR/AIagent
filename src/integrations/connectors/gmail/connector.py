"""Gmail standalone connector implementing BaseConnector."""

import logging
from typing import Any

from src.integrations.base import AuthType, BaseConnector, ConnectionHealth, ProviderMetadata
from src.integrations.connectors.gmail.client import GmailClient
from src.integrations.connectors.gmail.tools import get_gmail_tools
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class GmailConnector(BaseConnector):
    """Connector for Google Gmail services."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id="gmail",
            name="Gmail",
            description="Read, search, draft, and send emails with explicit confirmation.",
            auth_type=AuthType.OAUTH2,
            required_scopes=[
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.compose",
                "https://www.googleapis.com/auth/gmail.send",
            ],
            capabilities=[
                "search_emails",
                "read_messages",
                "create_drafts",
                "send_emails",
                "track_outreach",
            ],
            setup_guide="Connect via /connect google to link your Google account.",
            doc_url="https://developers.google.com/gmail/api",
        )

    async def test_connection(self, credentials: dict[str, Any]) -> ConnectionHealth:
        token = credentials.get("access_token", "")
        client = GmailClient(access_token=token)
        return await client.test_connection()

    async def get_tools(self, user_id: int, credentials: dict[str, Any]) -> list[BaseTool]:
        token = credentials.get("access_token", "")
        return get_gmail_tools(access_token=token, database_url=self.database_url)

