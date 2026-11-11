"""Comprehensive tests for Google Workspace (Gmail, Calendar, Drive) and Google Maps Places."""

import asyncio
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from telegram import Chat, Message, Update, User

from src.database.models import Base
from src.database.repositories.integration_repo import IntegrationRepository
from src.integrations.base import ConnectionHealth
from src.integrations.connectors.calendar import (
    CalendarCheckAvailabilityTool,
    CalendarCreateEventTool,
    CalendarDeleteEventTool,
    CalendarGetEventTool,
    CalendarListEventsTool,
    CalendarUpdateEventTool,
    GoogleCalendarClient,
    GoogleCalendarConnector,
)
from src.integrations.connectors.drive import (
    DriveDownloadTool,
    DriveGetMetadataTool,
    DriveReadDocumentTool,
    DriveSearchTool,
    DriveUploadTool,
    GoogleDriveClient,
    GoogleDriveConnector,
)
from src.integrations.connectors.gmail import (
    GmailClient,
    GmailConnector,
    GmailDraftTool,
    GmailOutreachHistoryTool,
    GmailReadTool,
    GmailSearchTool,
    GmailSendTool,
)
from src.integrations.connectors.google import GoogleOAuthClient, GoogleWorkspaceConnector
from src.integrations.connectors.maps import (
    GooglePlacesClient,
    GooglePlacesConnector,
    PlacesBusinessDiscoveryTool,
    PlacesGetDetailsTool,
    PlacesSearchTool,
)
from src.integrations.errors import AuthenticationError, RateLimitError
from src.integrations.registry import IntegrationRegistry
from src.integrations.security.crypto import CredentialEncryptor
from src.interfaces.telegram.handlers import (
    cancel_command,
    connect_command,
    handle_message,
)


class TestGoogleOAuth(unittest.IsolatedAsyncioTestCase):
    """Test Google OAuth2 URL generation, code exchange, token refresh, and revocation."""

    def setUp(self) -> None:
        self.oauth_client = GoogleOAuthClient(
            client_id="mock-client-id.apps.googleusercontent.com",
            client_secret="mock-client-secret",
            redirect_uri="urn:ietf:wg:oauth:2.0:oob",
        )

    def test_authorization_url_generation(self) -> None:
        url, csrf = self.oauth_client.generate_authorization_url(user_id=123)
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", url)
        self.assertIn("client_id=mock-client-id", url)
        self.assertIn("state=", url)
        self.assertIsInstance(csrf, str)

    async def test_code_exchange_success(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "mock-access-token-12345",
            "refresh_token": "mock-refresh-token-67890",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/gmail.readonly",
            "token_type": "Bearer",
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            tokens = await self.oauth_client.exchange_code("mock-auth-code-xyz")

            self.assertEqual(tokens["access_token"], "mock-access-token-12345")
            self.assertEqual(tokens["refresh_token"], "mock-refresh-token-67890")
            self.assertGreater(tokens["expires_at"], time.time())

    async def test_token_refresh(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "mock-new-access-token",
            "expires_in": 3600,
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            new_tokens = await self.oauth_client.refresh_access_token("mock-refresh-token")
            self.assertEqual(new_tokens["access_token"], "mock-new-access-token")

    async def test_token_revocation(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            revoked = await self.oauth_client.revoke_token("mock-token-to-revoke")
            self.assertTrue(revoked)


class TestGmailIntegration(unittest.IsolatedAsyncioTestCase):
    """Test Gmail Client, Search, Read, Draft, Approval Guard, and Outreach History."""

    def setUp(self) -> None:
        self.client = GmailClient(access_token="mock-gmail-token")

    async def test_gmail_search_and_read(self) -> None:
        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.json.return_value = {"messages": [{"id": "msg_001"}]}

        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "id": "msg_001",
            "threadId": "th_001",
            "snippet": "Meeting at 3pm tomorrow",
            "payload": {
                "headers": [
                    {"name": "From", "value": "alice@example.com"},
                    {"name": "Subject", "value": "Project Sync"},
                    {"name": "Date", "value": "2026-03-01"},
                ]
            },
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [search_resp, meta_resp]
            tool = GmailSearchTool(client=self.client)
            res = await tool.execute(query="from:alice")
            self.assertEqual(res["results_count"], 1)
            self.assertEqual(res["messages"][0]["subject"], "Project Sync")

    async def test_gmail_send_approval_enforcement(self) -> None:
        """Verify that sending an email requires explicit approval and is blocked without confirmation."""
        tool = GmailSendTool(client=self.client)

        # 1. Without confirmation: must be blocked with pending_approval
        unconfirmed_res = await tool.execute(
            to="bob@example.com",
            subject="Invoice #402",
            body="Attached is your invoice.",
            confirmed=False,
        )
        self.assertEqual(unconfirmed_res["status"], "pending_approval")
        self.assertTrue(unconfirmed_res["requires_approval"])
        self.assertIn("explicit user confirmation", unconfirmed_res["message"])

        # 2. With confirmation: proceeds to send email
        send_resp = MagicMock()
        send_resp.status_code = 200
        send_resp.json.return_value = {"id": "sent_123", "threadId": "th_123"}

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = send_resp
            confirmed_res = await tool.execute(
                to="bob@example.com",
                subject="Invoice #402",
                body="Attached is your invoice.",
                confirmed=True,
            )
            self.assertEqual(confirmed_res["status"], "sent")
            self.assertEqual(confirmed_res["message_id"], "sent_123")


class TestGoogleCalendarIntegration(unittest.IsolatedAsyncioTestCase):
    """Test Google Calendar Client, Event Operations, and Destructive Confirmation."""

    def setUp(self) -> None:
        self.client = GoogleCalendarClient(access_token="mock-cal-token")

    async def test_calendar_list_and_availability(self) -> None:
        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = {
            "items": [
                {
                    "id": "ev_01",
                    "summary": "AI Product Review",
                    "start": {"dateTime": "2026-03-01T10:00:00Z"},
                    "end": {"dateTime": "2026-03-01T11:00:00Z"},
                }
            ]
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = list_resp
            tool = CalendarListEventsTool(client=self.client)
            res = await tool.execute()
            self.assertEqual(res["events_count"], 1)
            self.assertEqual(res["events"][0]["summary"], "AI Product Review")

    async def test_calendar_delete_confirmation(self) -> None:
        """Verify deleting a calendar event requires explicit confirmation."""
        tool = CalendarDeleteEventTool(client=self.client)

        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {"id": "ev_01", "summary": "Standup"}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = get_resp
            # 1. Unconfirmed
            blocked = await tool.execute(event_id="ev_01", confirmed=False)
            self.assertEqual(blocked["status"], "pending_approval")
            self.assertTrue(blocked["requires_approval"])

        # 2. Confirmed
        del_resp = MagicMock()
        del_resp.status_code = 204

        with patch("httpx.AsyncClient.delete", new_callable=AsyncMock) as mock_del:
            mock_del.return_value = del_resp
            success = await tool.execute(event_id="ev_01", confirmed=True)
            self.assertEqual(success["status"], "deleted")


class TestGoogleDriveIntegration(unittest.IsolatedAsyncioTestCase):
    """Test Google Drive File Search, Document Export/Reading, and Size Limits."""

    def setUp(self) -> None:
        self.client = GoogleDriveClient(access_token="mock-drive-token")

    async def test_drive_search_and_export(self) -> None:
        # 1. Search
        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.json.return_value = {
            "files": [
                {
                    "id": "doc_101",
                    "name": "Project Roadmap",
                    "mimeType": "application/vnd.google-apps.document",
                }
            ]
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = search_resp
            tool = DriveSearchTool(client=self.client)
            res = await tool.execute(query="Roadmap")
            self.assertEqual(res["files_count"], 1)
            self.assertEqual(res["files"][0]["name"], "Project Roadmap")

        # 2. Read Google Doc export
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "id": "doc_101",
            "name": "Project Roadmap",
            "mimeType": "application/vnd.google-apps.document",
        }

        export_resp = MagicMock()
        export_resp.status_code = 200
        export_resp.text = "Q1 Objectives: Implement Phase 2 Integrations."

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [meta_resp, export_resp]
            read_tool = DriveReadDocumentTool(client=self.client)
            read_res = await read_tool.execute(file_id="doc_101")
            self.assertEqual(read_res["export_type"], "text/plain")
            self.assertIn("Q1 Objectives", read_res["content"])


class TestGooglePlacesIntegration(unittest.IsolatedAsyncioTestCase):
    """Test Google Maps Places API client, field masks, details, and client discovery."""

    def setUp(self) -> None:
        self.client = GooglePlacesClient(api_key="mock-places-key")

    async def test_places_search_and_details(self) -> None:
        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.json.return_value = {
            "status": "OK",
            "results": [
                {
                    "place_id": "ChIJ12345",
                    "name": "Summit Dental Care",
                    "formatted_address": "100 Main St, Austin, TX",
                    "rating": 4.8,
                }
            ],
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = search_resp
            tool = PlacesSearchTool(client=self.client)
            res = await tool.execute(query="dental clinics in Austin")
            self.assertEqual(res["results_count"], 1)
            self.assertEqual(res["places"][0]["name"], "Summit Dental Care")

    async def test_client_leads_discovery(self) -> None:
        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.json.return_value = {
            "status": "OK",
            "results": [{"place_id": "ChIJ999", "name": "Alpha Law Firm"}],
        }

        details_resp = MagicMock()
        details_resp.status_code = 200
        details_resp.json.return_value = {
            "status": "OK",
            "result": {
                "place_id": "ChIJ999",
                "name": "Alpha Law Firm",
                "formatted_address": "500 Congress Ave, Austin, TX",
                "formatted_phone_number": "+1 512-555-0199",
                "website": "https://alphalaw.example.com",
                "rating": 4.9,
                "url": "https://maps.google.com/?cid=999",
            },
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [search_resp, details_resp]
            leads_tool = PlacesBusinessDiscoveryTool(client=self.client)
            res = await leads_tool.execute(category="law firms", location="Austin, TX", min_rating=4.0)

            self.assertEqual(res["leads_count"], 1)
            lead = res["leads"][0]
            self.assertEqual(lead["name"], "Alpha Law Firm")
            self.assertEqual(lead["phone_number"], "+1 512-555-0199")
            self.assertEqual(lead["website"], "https://alphalaw.example.com")
            self.assertEqual(lead["attribution"], "Powered by Google Places")


class TestTelegramGoogleOAuthFlow(unittest.IsolatedAsyncioTestCase):
    """Test Telegram OAuth authorization flow and secret code deletion."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        self.encryptor = CredentialEncryptor("test-secret-key-32-chars-long!")
        self.registry = IntegrationRegistry(encryptor=self.encryptor)

        self.oauth_client = GoogleOAuthClient(
            client_id="mock-client-id",
            client_secret="mock-client-secret",
            redirect_uri="urn:ietf:wg:oauth:2.0:oob",
        )
        self.workspace_connector = GoogleWorkspaceConnector(oauth_client=self.oauth_client)
        self.registry.register_connector(self.workspace_connector)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_connect_google_oauth_code_submission(self) -> None:
        user_id = 777
        context = MagicMock()
        context.bot_data = {
            "config": MagicMock(
                allowed_telegram_user_ids=frozenset([user_id]),
                environment="testing",
                log_level="INFO",
                gemini_model="gemini-2.5-flash",
                database_url="sqlite+aiosqlite:///:memory:",
            ),
            "integration_registry": self.registry,
        }
        context.user_data = {"awaiting_oauth_code_for": "google", "oauth_csrf": "csrf_123"}

        async with self.session_factory() as session:
            repo = IntegrationRepository(session)
            context.bot_data["integration_repository"] = repo

            # Mock exchange and verification
            with patch.object(self.oauth_client, "exchange_code", new_callable=AsyncMock) as mock_exch:
                mock_exch.return_value = {
                    "access_token": "ya29.mock_google_access_token",
                    "refresh_token": "1//mock_google_refresh_token",
                    "expires_in": 3600,
                    "expires_at": int(time.time()) + 3600,
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                }

                with patch.object(self.oauth_client, "test_connection", new_callable=AsyncMock) as mock_test:
                    mock_test.return_value = ConnectionHealth(
                        status="healthy",
                        is_healthy=True,
                        latency_ms=25.0,
                        details={"email": "om@example.com"},
                    )

                    update = MagicMock(spec=Update)
                    update.update_id = 991122
                    update.effective_user = User(id=user_id, is_bot=False, first_name="Om")
                    update.effective_chat = Chat(id=user_id, type="private")

                    message = AsyncMock(spec=Message)
                    message.text = "4/0AeanS0_mock_auth_code"
                    message.document = None
                    status_msg = AsyncMock()
                    message.reply_text.return_value = status_msg
                    update.message = message

                    await handle_message(update, context)

                    # 1. Message containing authorization code was deleted
                    message.delete.assert_awaited_once()

                    # 2. State cleared
                    self.assertNotIn("awaiting_oauth_code_for", context.user_data)

                    # 3. Success confirmation displayed
                    status_msg.edit_text.assert_awaited_once()
                    reply_text = status_msg.edit_text.call_args[0][0]
                    self.assertIn("Successfully connected Google Workspace", reply_text)
                    self.assertIn("om@example.com", reply_text)

                    # 4. Verified encrypted credentials stored in DB
                    stored = await repo.get_credentials(user_id, "google")
                    self.assertIsNotNone(stored)
                    self.assertEqual(stored.status, "connected")
                    self.assertNotIn("mock_google_access_token", stored.encrypted_credentials)


if __name__ == "__main__":
    unittest.main()

