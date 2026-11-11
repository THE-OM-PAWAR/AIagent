"""Asynchronous REST client for the Google Drive API v3."""

import json
import logging
from pathlib import Path
import re
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

logger = logging.getLogger(__name__)

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"

# Google Doc MIME types mapping to export formats
GOOGLE_DOC_EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}

MAX_DRIVE_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20MB safety limit


class GoogleDriveClient:
    """Asynchronous client interacting with Google Drive API v3."""

    def __init__(
        self,
        access_token: str,
        timeout: float = 20.0,
    ) -> None:
        self.access_token = access_token.strip()
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    def _handle_response_error(self, resp: httpx.Response) -> None:
        """Translate HTTP status codes to typed IntegrationErrors."""
        if resp.status_code in (401, 403):
            logger.warning("Google Drive authentication failure: %s", resp.text)
            raise AuthenticationError("Google Drive authentication failed or token expired.", provider_id="google_drive")
        if resp.status_code == 429:
            logger.warning("Google Drive rate limit exceeded.")
            raise RateLimitError("Google Drive quota or rate limit exceeded.", provider_id="google_drive")
        if resp.status_code >= 500:
            logger.warning("Google Drive server error: %s", resp.status_code)
            raise ProviderUnavailableError(f"Google Drive service unavailable ({resp.status_code}).", provider_id="google_drive")
        raise IntegrationError(f"Google Drive error ({resp.status_code}): {resp.text[:200]}", provider_id="google_drive")

    async def test_connection(self) -> ConnectionHealth:
        """Verify token validity by querying Drive about endpoint."""
        if not self.access_token:
            return ConnectionHealth(status="unauthorized", is_healthy=False, error_message="Access token is empty.")

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{DRIVE_API_BASE}/about?fields=user,storageQuota", headers=self._headers)
                latency = round((time.perf_counter() - start) * 1000.0, 1)

                if resp.status_code == 200:
                    data = resp.json()
                    user_info = data.get("user", {})
                    email = user_info.get("emailAddress", "unknown")
                    return ConnectionHealth(
                        status="healthy",
                        is_healthy=True,
                        latency_ms=latency,
                        details={"email": email, "displayName": user_info.get("displayName")},
                    )
                if resp.status_code in (401, 403):
                    return ConnectionHealth(
                        status="unauthorized",
                        is_healthy=False,
                        latency_ms=latency,
                        error_message="Drive access token invalid or expired.",
                    )
                return ConnectionHealth(
                    status="degraded",
                    is_healthy=False,
                    latency_ms=latency,
                    error_message=f"Drive check returned status {resp.status_code}.",
                )
        except Exception as exc:
            latency = round((time.perf_counter() - start) * 1000.0, 1)
            return ConnectionHealth(
                status="unhealthy",
                is_healthy=False,
                latency_ms=latency,
                error_message=f"Network error verifying Drive: {exc}",
            )

    async def search_files(
        self,
        query: str = "",
        page_size: int = 15,
        mime_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search files matching query string and optional MIME type filter."""
        conditions = ["trashed = false"]
        if query.strip():
            safe_q = query.replace("'", "\\'")
            conditions.append(f"(name contains '{safe_q}' or fullText contains '{safe_q}')")
        if mime_type:
            conditions.append(f"mimeType = '{mime_type}'")

        q_str = " and ".join(conditions)
        params = {
            "q": q_str,
            "pageSize": min(max(1, page_size), 50),
            "fields": "files(id,name,mimeType,size,modifiedTime,owners,webViewLink)",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{DRIVE_API_BASE}/files", headers=self._headers, params=params)
            if resp.status_code != 200:
                self._handle_response_error(resp)

            files = resp.json().get("files", [])
            return [
                {
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "mime_type": f.get("mimeType"),
                    "size_bytes": int(f["size"]) if f.get("size") else None,
                    "modified_time": f.get("modifiedTime"),
                    "web_view_link": f.get("webViewLink"),
                    "owner": f.get("owners", [{}])[0].get("displayName") if f.get("owners") else None,
                }
                for f in files
            ]

    async def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        """Retrieve detailed metadata for a file."""
        params = {"fields": "id,name,mimeType,size,modifiedTime,createdTime,owners,shared,webViewLink,description"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{DRIVE_API_BASE}/files/{file_id}", headers=self._headers, params=params)
            if resp.status_code != 200:
                self._handle_response_error(resp)

            f = resp.json()
            return {
                "id": f.get("id"),
                "name": f.get("name"),
                "mime_type": f.get("mimeType"),
                "size_bytes": int(f["size"]) if f.get("size") else None,
                "created_time": f.get("createdTime"),
                "modified_time": f.get("modifiedTime"),
                "shared": f.get("shared", False),
                "web_view_link": f.get("webViewLink"),
                "description": f.get("description", ""),
            }

    async def read_document(self, file_id: str) -> dict[str, Any]:
        """Read text or exported content from Google Doc, Sheet, or text file."""
        meta = await self.get_file_metadata(file_id)
        mime_type = meta.get("mime_type", "")
        file_name = meta.get("name", file_id)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # 1. Google Workspace Doc / Sheet / Slides export
            if mime_type in GOOGLE_DOC_EXPORTS:
                export_mime, ext = GOOGLE_DOC_EXPORTS[mime_type]
                resp = await client.get(
                    f"{DRIVE_API_BASE}/files/{file_id}/export",
                    headers=self._headers,
                    params={"mimeType": export_mime},
                )
                if resp.status_code != 200:
                    self._handle_response_error(resp)

                content = resp.text
                return {
                    "id": file_id,
                    "name": file_name,
                    "mime_type": mime_type,
                    "export_type": export_mime,
                    "content": content[:20000],  # Return up to 20K characters for prompt context
                    "truncated": len(content) > 20000,
                    "total_chars": len(content),
                }

            # 2. Binary / direct download for plain text, csv, json, markdown
            size = meta.get("size_bytes") or 0
            if size > MAX_DRIVE_DOWNLOAD_BYTES:
                raise IntegrationError(f"File size ({size} bytes) exceeds maximum supported limit of 20MB.", provider_id="google_drive")

            resp = await client.get(
                f"{DRIVE_API_BASE}/files/{file_id}",
                headers=self._headers,
                params={"alt": "media"},
            )
            if resp.status_code != 200:
                self._handle_response_error(resp)

            # Try decoding text
            try:
                content = resp.text
                return {
                    "id": file_id,
                    "name": file_name,
                    "mime_type": mime_type,
                    "content": content[:20000],
                    "truncated": len(content) > 20000,
                    "total_chars": len(content),
                }
            except Exception:
                return {
                    "id": file_id,
                    "name": file_name,
                    "mime_type": mime_type,
                    "message": "Binary document content cannot be previewed directly as text. Use drive_download_file instead.",
                    "size_bytes": len(resp.content),
                }

    async def download_file(
        self,
        file_id: str,
        destination_dir: str = "data/uploads",
    ) -> dict[str, Any]:
        """Download file from Google Drive to local workspace directory."""
        meta = await self.get_file_metadata(file_id)
        raw_name = meta.get("name", f"drive_{file_id}")
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw_name)
        mime_type = meta.get("mime_type", "")

        dest_path = Path(destination_dir)
        dest_path.mkdir(parents=True, exist_ok=True)
        target_file = dest_path / safe_name

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # Handle Google Doc export download
            if mime_type in GOOGLE_DOC_EXPORTS:
                export_mime, ext = GOOGLE_DOC_EXPORTS[mime_type]
                if not target_file.suffix:
                    target_file = target_file.with_suffix(ext)

                resp = await client.get(
                    f"{DRIVE_API_BASE}/files/{file_id}/export",
                    headers=self._headers,
                    params={"mimeType": export_mime},
                )
                if resp.status_code != 200:
                    self._handle_response_error(resp)

                target_file.write_bytes(resp.content)
                return {
                    "id": file_id,
                    "name": raw_name,
                    "local_path": str(target_file),
                    "bytes_downloaded": len(resp.content),
                    "exported_as": export_mime,
                }

            # Standard direct download
            size = meta.get("size_bytes") or 0
            if size > MAX_DRIVE_DOWNLOAD_BYTES:
                raise IntegrationError(f"File size exceeds 20MB limit.", provider_id="google_drive")

            resp = await client.get(
                f"{DRIVE_API_BASE}/files/{file_id}",
                headers=self._headers,
                params={"alt": "media"},
            )
            if resp.status_code != 200:
                self._handle_response_error(resp)

            target_file.write_bytes(resp.content)
            return {
                "id": file_id,
                "name": raw_name,
                "local_path": str(target_file),
                "bytes_downloaded": len(resp.content),
            }

    async def upload_file(
        self,
        file_path: str,
        name: str | None = None,
        folder_id: str | None = None,
        mime_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Upload a local file to Google Drive."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = path.stat().st_size
        if file_size > MAX_DRIVE_DOWNLOAD_BYTES:
            raise IntegrationError("File exceeds 20MB limit.", provider_id="google_drive")

        upload_name = name or path.name
        metadata: dict[str, Any] = {"name": upload_name}
        if folder_id:
            metadata["parents"] = [folder_id]

        # Multipart upload: Part 1 is JSON metadata, Part 2 is file bytes
        boundary = "===============7330837588304934521=="
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{DRIVE_UPLOAD_BASE}/files?uploadType=multipart",
                headers=headers,
                content=body,
            )
            if resp.status_code not in (200, 201):
                self._handle_response_error(resp)

            data = resp.json()
            return {
                "id": data.get("id"),
                "name": data.get("name"),
                "mime_type": data.get("mimeType"),
                "status": "uploaded",
            }

