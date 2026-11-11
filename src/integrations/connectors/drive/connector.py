"""Google Drive standalone connector implementing BaseConnector."""

import logging
from typing import Any

from src.integrations.base import AuthType, BaseConnector, ConnectionHealth, ProviderMetadata
from src.integrations.connectors.drive.client import GoogleDriveClient
from src.integrations.connectors.drive.tools import get_drive_tools
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class GoogleDriveConnector(BaseConnector):
    """Connector for Google Drive storage service."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id="google_drive",
            name="Google Drive",
            description="Search files, read documents, export spreadsheets, and download/upload files.",
            auth_type=AuthType.OAUTH2,
            required_scopes=[
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/drive.file",
            ],
            capabilities=[
                "search_files",
                "read_documents",
                "download_files",
                "upload_files",
            ],
            setup_guide="Connect via /connect google to link your Google Drive.",
            doc_url="https://developers.google.com/drive/api",
        )

    async def test_connection(self, credentials: dict[str, Any]) -> ConnectionHealth:
        token = credentials.get("access_token", "")
        client = GoogleDriveClient(access_token=token)
        return await client.test_connection()

    async def get_tools(self, user_id: int, credentials: dict[str, Any]) -> list[BaseTool]:
        token = credentials.get("access_token", "")
        return get_drive_tools(access_token=token, database_url=self.database_url)

