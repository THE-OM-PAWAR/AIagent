"""Google Drive tool implementations exposing BaseTool interfaces."""

import logging
from typing import Any

from src.integrations.connectors.drive.client import GoogleDriveClient
from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)


async def _resolve_client(
    client: GoogleDriveClient | None,
    user_id: int | None,
    integration_registry: Any | None,
    database_url: str | None,
) -> GoogleDriveClient | None:
    """Resolve active GoogleDriveClient dynamically using user credentials if available."""
    if user_id and integration_registry and database_url:
        try:
            from src.database.repositories.integration_repo import IntegrationRepository
            from src.database.session import get_async_session

            async with get_async_session(database_url) as session:
                repo = IntegrationRepository(session)
                creds = await integration_registry.get_credentials(repo, user_id, "google_drive")
                if not creds:
                    creds = await integration_registry.get_credentials(repo, user_id, "google")

                if creds and creds.get("access_token"):
                    return GoogleDriveClient(access_token=creds["access_token"])
        except Exception as exc:
            logger.debug("Failed resolving user Drive credentials: %s", exc)

    return client


class DriveSearchTool(BaseTool):
    """Search files in Google Drive."""

    def __init__(
        self,
        client: GoogleDriveClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "drive_search_files"

    @property
    def description(self) -> str:
        return "Search files and folders in Google Drive by filename, full-text keywords, or MIME type."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keyword or filename substring."},
                "mime_type": {"type": "string", "description": "Optional MIME type filter (e.g. 'application/pdf', 'application/vnd.google-apps.document')."},
                "page_size": {"type": "integer", "description": "Maximum number of files to return (default: 10).", "default": 10},
            },
        }

    async def execute(
        self,
        query: str = "",
        mime_type: str | None = None,
        page_size: int = 10,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Google Drive is not connected. Use /connect google in Telegram to connect."}

        files = await active_client.search_files(query=query, page_size=page_size, mime_type=mime_type)
        return {"files_count": len(files), "files": files}


class DriveGetMetadataTool(BaseTool):
    """Retrieve metadata for a Google Drive file."""

    def __init__(
        self,
        client: GoogleDriveClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "drive_get_file_metadata"

    @property
    def description(self) -> str:
        return "Retrieve metadata for a file in Google Drive, including name, size, modified time, owner, and web link."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Unique Google Drive file ID."},
            },
            "required": ["file_id"],
        }

    async def execute(self, file_id: str, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Google Drive is not connected. Use /connect google in Telegram to connect."}

        return await active_client.get_file_metadata(file_id=file_id)


class DriveReadDocumentTool(BaseTool):
    """Read document text content from Google Drive."""

    def __init__(
        self,
        client: GoogleDriveClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "drive_read_document"

    @property
    def description(self) -> str:
        return "Read text content from a Google Doc, Google Sheet, CSV, JSON, or text file stored in Google Drive."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Unique Google Drive file ID to read."},
            },
            "required": ["file_id"],
        }

    async def execute(self, file_id: str, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Google Drive is not connected. Use /connect google in Telegram to connect."}

        return await active_client.read_document(file_id=file_id)


class DriveDownloadTool(BaseTool):
    """Download a file from Google Drive to local workspace."""

    def __init__(
        self,
        client: GoogleDriveClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "drive_download_file"

    @property
    def description(self) -> str:
        return "Download a file or export a Google Doc from Google Drive to the local agent workspace for processing."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Unique Google Drive file ID to download."},
                "destination_dir": {
                    "type": "string",
                    "description": "Local directory path to save file (default: 'data/uploads').",
                    "default": "data/uploads",
                },
            },
            "required": ["file_id"],
        }

    async def execute(
        self,
        file_id: str,
        destination_dir: str = "data/uploads",
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Google Drive is not connected. Use /connect google in Telegram to connect."}

        # User directory segregation
        target_dir = f"{destination_dir}/{user_id}" if user_id else destination_dir
        return await active_client.download_file(file_id=file_id, destination_dir=target_dir)


class DriveUploadTool(BaseTool):
    """Upload a local file to Google Drive."""

    def __init__(
        self,
        client: GoogleDriveClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "drive_upload_file"

    @property
    def description(self) -> str:
        return "Upload a local report, spreadsheet, or file from the agent workspace to the user's Google Drive."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Local workspace path of the file to upload."},
                "name": {"type": "string", "description": "Optional target filename on Google Drive."},
                "folder_id": {"type": "string", "description": "Optional Google Drive folder ID to upload into."},
            },
            "required": ["file_path"],
        }

    async def execute(
        self,
        file_path: str,
        name: str | None = None,
        folder_id: str | None = None,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.access_token:
            return {"error": "Google Drive is not connected. Use /connect google in Telegram to connect."}

        return await active_client.upload_file(file_path=file_path, name=name, folder_id=folder_id)


def get_drive_tools(
    access_token: str | None = None,
    client: GoogleDriveClient | None = None,
    integration_registry: Any | None = None,
    database_url: str | None = None,
) -> list[BaseTool]:
    """Factory creating all Google Drive tools configured with credentials."""
    drive_client = client or (GoogleDriveClient(access_token=access_token) if access_token else None)
    return [
        DriveSearchTool(client=drive_client, integration_registry=integration_registry, database_url=database_url),
        DriveGetMetadataTool(client=drive_client, integration_registry=integration_registry, database_url=database_url),
        DriveReadDocumentTool(client=drive_client, integration_registry=integration_registry, database_url=database_url),
        DriveDownloadTool(client=drive_client, integration_registry=integration_registry, database_url=database_url),
        DriveUploadTool(client=drive_client, integration_registry=integration_registry, database_url=database_url),
    ]

