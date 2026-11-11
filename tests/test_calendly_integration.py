"""Comprehensive tests for Calendly integration (OAuth, PAT, Client, Webhooks, and Tools)."""

import asyncio
import hmac
import hashlib
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from telegram import Chat, Message, Update, User

from src.database.models import Base
from src.database.repositories.integration_repo import IntegrationRepository
from src.integrations.base import AuthType, ConnectionHealth
from src.integrations.connectors.calendly.client import CalendlyClient
from src.integrations.connectors.calendly.connector import CalendlyConnector
from src.integrations.connectors.calendly.oauth import CalendlyOAuthClient
from src.integrations.connectors.calendly.tools import (
    CalendlyBookingUpdatesTool,
    CalendlyGetCurrentUserTool,
    CalendlyGetInviteesTool,
    CalendlyGetSchedulingLinkTool,
    CalendlyListEventTypesTool,
    CalendlyListScheduledEventsTool,
    get_calendly_tools,
)
from src.integrations.connectors.calendly.webhook import (
    WebhookDeduplicator,
    verify_calendly_webhook_signature,
)
from src.integrations.errors import (
    AuthenticationError,
    WebhookVerificationError,
)
from src.integrations.registry import IntegrationRegistry
from src.integrations.security.crypto import CredentialEncryptor
from src.interfaces.telegram.handlers import (
    cancel_command,
    connect_command,
    handle_message,
)


class TestCalendlyOAuthClient(unittest.IsolatedAsyncioTestCase):
    """Unit tests for Calendly OAuth2 operations."""

    def setUp(self) -> None:
        self.oauth_client = CalendlyOAuthClient(
            client_id="test-calendly-client-id",
            client_secret="test-calendly-client-secret",
            redirect_uri="https://app.example.com/oauth/calendly/callback",
        )

    def test_generate_authorization_url(self) -> None:
        url, csrf = self.oauth_client.generate_authorization_url(user_id=123)
        self.assertIn("https://auth.calendly.com/oauth/authorize", url)
        self.assertIn("client_id=test-calendly-client-id", url)
        self.assertIn("response_type=code", url)
        self.assertTrue(bool(csrf))

    def test_generate_authorization_url_missing_client_id(self) -> None:
        unconfigured = CalendlyOAuthClient()
        with self.assertRaises(AuthenticationError):
            unconfigured.generate_authorization_url(user_id=123)

    @patch("src.integrations.http_client.IntegrationHttpClient.post")
    async def test_exchange_code(self, mock_post: AsyncMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "cal_at_12345",
            "refresh_token": "cal_rt_67890",
            "token_type": "Bearer",
            "expires_in": 7200,
            "owner": "https://api.calendly.com/users/USER_UUID_123",
            "organization": "https://api.calendly.com/organizations/ORG_UUID_123",
        }
        mock_post.return_value = mock_response

        tokens = await self.oauth_client.exchange_code("auth_code_xyz")
        self.assertEqual(tokens["access_token"], "cal_at_12345")
        self.assertEqual(tokens["refresh_token"], "cal_rt_67890")
        self.assertEqual(tokens["owner"], "https://api.calendly.com/users/USER_UUID_123")
        self.assertIn("expires_at", tokens)

    @patch("src.integrations.http_client.IntegrationHttpClient.post")
    async def test_refresh_access_token(self, mock_post: AsyncMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "cal_at_new_123",
            "refresh_token": "cal_rt_new_456",
            "expires_in": 3600,
        }
        mock_post.return_value = mock_response

        tokens = await self.oauth_client.refresh_access_token("cal_rt_old")
        self.assertEqual(tokens["access_token"], "cal_at_new_123")
        self.assertEqual(tokens["refresh_token"], "cal_rt_new_456")

    @patch("src.integrations.http_client.IntegrationHttpClient.post")
    async def test_revoke_token(self, mock_post: AsyncMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = await self.oauth_client.revoke_token("cal_at_12345")
        self.assertTrue(result)


class TestCalendlyClient(unittest.IsolatedAsyncioTestCase):
    """Unit tests for CalendlyClient API interactions."""

    def setUp(self) -> None:
        self.client = CalendlyClient(token="test_cal_token_123")

    @patch("src.integrations.http_client.IntegrationHttpClient.get")
    async def test_get_current_user(self, mock_get: AsyncMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "resource": {
                "uri": "https://api.calendly.com/users/USER_UUID_123",
                "name": "Alex Founder",
                "email": "alex@startup.com",
                "scheduling_url": "https://calendly.com/alex-founder",
                "timezone": "America/New_York",
                "current_organization": "https://api.calendly.com/organizations/ORG_UUID_123",
            }
        }
        mock_get.return_value = mock_response

        user = await self.client.get_current_user()
        self.assertEqual(user["name"], "Alex Founder")
        self.assertEqual(user["email"], "alex@startup.com")
        self.assertEqual(user["scheduling_url"], "https://calendly.com/alex-founder")

    @patch("src.integrations.http_client.IntegrationHttpClient.get")
    async def test_list_event_types(self, mock_get: AsyncMock) -> None:
        mock_get.side_effect = [
            # 1. get_current_user
            MagicMock(
                status_code=200,
                json=lambda: {"resource": {"uri": "https://api.calendly.com/users/USER_123"}},
            ),
            # 2. list_event_types
            MagicMock(
                status_code=200,
                json=lambda: {
                    "collection": [
                        {
                            "uri": "https://api.calendly.com/event_types/ET_1",
                            "name": "30 Min Discovery Chat",
                            "active": True,
                            "scheduling_url": "https://calendly.com/alex/30min",
                            "duration": 30,
                            "kind": "solo",
                        }
                    ]
                },
            ),
        ]

        types = await self.client.list_event_types()
        self.assertEqual(len(types), 1)
        self.assertEqual(types[0]["name"], "30 Min Discovery Chat")
        self.assertEqual(types[0]["duration_minutes"], 30)

    @patch("src.integrations.http_client.IntegrationHttpClient.post")
    async def test_get_scheduling_link(self, mock_post: AsyncMock) -> None:
        mock_post.return_value = MagicMock(
            status_code=201,
            json=lambda: {
                "resource": {
                    "booking_url": "https://calendly.com/d/abc-123/30-minute-meeting",
                    "owner": "https://api.calendly.com/event_types/ET_1",
                    "owner_type": "EventType",
                }
            },
        )

        link_data = await self.client.get_scheduling_link("https://api.calendly.com/event_types/ET_1", max_event_count=1)
        self.assertIn("booking_url", link_data)
        self.assertEqual(link_data["booking_url"], "https://calendly.com/d/abc-123/30-minute-meeting")

    @patch("src.integrations.http_client.IntegrationHttpClient.get")
    async def test_list_scheduled_events(self, mock_get: AsyncMock) -> None:
        mock_get.side_effect = [
            # get_current_user
            MagicMock(
                status_code=200,
                json=lambda: {"resource": {"uri": "https://api.calendly.com/users/USER_123"}},
            ),
            # scheduled_events
            MagicMock(
                status_code=200,
                json=lambda: {
                    "collection": [
                        {
                            "uri": "https://api.calendly.com/scheduled_events/EV_1",
                            "name": "Investor Sync",
                            "status": "active",
                            "start_time": "2026-11-15T15:00:00Z",
                            "end_time": "2026-11-15T15:30:00Z",
                        }
                    ]
                },
            ),
        ]

        events = await self.client.list_scheduled_events(status="active")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "Investor Sync")
        self.assertEqual(events[0]["status"], "active")

    @patch("src.integrations.http_client.IntegrationHttpClient.get")
    async def test_get_invitees(self, mock_get: AsyncMock) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "collection": [
                    {
                        "uri": "https://api.calendly.com/scheduled_events/EV_1/invitees/INV_1",
                        "email": "investor@sequoia.com",
                        "name": "Jane Partner",
                        "status": "active",
                        "timezone": "America/Los_Angeles",
                    }
                ]
            },
        )

        invitees = await self.client.get_invitees("EV_1")
        self.assertEqual(len(invitees), 1)
        self.assertEqual(invitees[0]["email"], "investor@sequoia.com")
        self.assertEqual(invitees[0]["name"], "Jane Partner")

    @patch("src.integrations.http_client.IntegrationHttpClient.get")
    async def test_test_connection_healthy(self, mock_get: AsyncMock) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "resource": {
                    "uri": "https://api.calendly.com/users/USER_123",
                    "name": "Alex Founder",
                    "email": "alex@startup.com",
                }
            },
        )

        health = await self.client.test_connection()
        self.assertTrue(health.is_healthy)
        self.assertEqual(health.status, "healthy")
        self.assertEqual(health.details.get("email"), "alex@startup.com")


class TestCalendlyWebhooks(unittest.TestCase):
    """Unit tests for Calendly webhook signature verification and deduplication."""

    def setUp(self) -> None:
        self.signing_key = "test_webhook_secret_key_123"

    def _generate_header(self, payload: str, timestamp: int, key: str) -> str:
        data_to_sign = f"{timestamp}.{payload}"
        signature = hmac.new(
            key.encode("utf-8"),
            data_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"t={timestamp},v1={signature}"

    def test_verify_valid_signature(self) -> None:
        payload = json.dumps({"event": "invitee.created", "payload": {"uuid": "BOOKING-123"}})
        timestamp = int(time.time())
        header = self._generate_header(payload, timestamp, self.signing_key)

        valid = verify_calendly_webhook_signature(
            signature_header=header,
            raw_body=payload,
            signing_key=self.signing_key,
        )
        self.assertTrue(valid)

    def test_verify_invalid_signature_raises(self) -> None:
        payload = json.dumps({"event": "invitee.created", "payload": {"uuid": "BOOKING-123"}})
        timestamp = int(time.time())
        header = f"t={timestamp},v1=invalidsignatureabcdef123456"

        with self.assertRaises(WebhookVerificationError):
            verify_calendly_webhook_signature(
                signature_header=header,
                raw_body=payload,
                signing_key=self.signing_key,
            )

    def test_verify_expired_timestamp_raises(self) -> None:
        payload = json.dumps({"event": "invitee.created"})
        # 400 seconds ago (tolerance default is 300)
        old_timestamp = int(time.time()) - 400
        header = self._generate_header(payload, old_timestamp, self.signing_key)

        with self.assertRaises(WebhookVerificationError):
            verify_calendly_webhook_signature(
                signature_header=header,
                raw_body=payload,
                signing_key=self.signing_key,
                tolerance_seconds=300,
            )

    def test_verify_missing_header_raises(self) -> None:
        with self.assertRaises(WebhookVerificationError):
            verify_calendly_webhook_signature(
                signature_header="",
                raw_body="{}",
                signing_key=self.signing_key,
            )

    def test_webhook_deduplication(self) -> None:
        dedup = WebhookDeduplicator(capacity=100)
        event_id = "BOOKING-999"

        # First time seen: not duplicate
        self.assertFalse(dedup.is_duplicate(event_id))

        # Second time seen: duplicate
        self.assertTrue(dedup.is_duplicate(event_id))

        # Different event: not duplicate
        self.assertFalse(dedup.is_duplicate("BOOKING-888"))


class TestCalendlyTools(unittest.IsolatedAsyncioTestCase):
    """Unit tests for Calendly tools exposed to the agent."""

    def setUp(self) -> None:
        self.mock_client = MagicMock(spec=CalendlyClient)
        self.mock_client.token = "test_token"

    async def test_get_current_user_tool(self) -> None:
        self.mock_client.get_current_user = AsyncMock(return_value={
            "name": "Sarah Architect",
            "email": "sarah@tech.io",
            "scheduling_url": "https://calendly.com/sarah",
            "timezone": "UTC",
        })
        tool = CalendlyGetCurrentUserTool(client=self.mock_client)
        result = await tool.execute()
        self.assertEqual(result["name"], "Sarah Architect")
        self.assertEqual(result["email"], "sarah@tech.io")

    async def test_list_event_types_tool(self) -> None:
        self.mock_client.list_event_types = AsyncMock(return_value=[
            {"name": "Quick Chat", "active": True, "scheduling_url": "https://calendly.com/sarah/quick"}
        ])
        tool = CalendlyListEventTypesTool(client=self.mock_client)
        result = await tool.execute()
        self.assertEqual(result["event_types_count"], 1)
        self.assertEqual(len(result["event_types"]), 1)

    async def test_get_scheduling_link_tool(self) -> None:
        self.mock_client.get_scheduling_link = AsyncMock(return_value={"booking_url": "https://calendly.com/d/single-use-link"})
        tool = CalendlyGetSchedulingLinkTool(client=self.mock_client)
        result = await tool.execute(event_type_uri="https://api.calendly.com/event_types/123", max_event_count=1)
        self.assertEqual(result["booking_url"], "https://calendly.com/d/single-use-link")

    async def test_list_scheduled_events_tool(self) -> None:
        self.mock_client.list_scheduled_events = AsyncMock(return_value=[
            {"name": "Demo Meeting", "status": "active", "start_time": "2026-11-20T10:00:00Z"}
        ])
        tool = CalendlyListScheduledEventsTool(client=self.mock_client)
        result = await tool.execute(status="active")
        self.assertEqual(result["events_count"], 1)
        self.assertEqual(len(result["events"]), 1)

    async def test_get_invitees_tool(self) -> None:
        self.mock_client.get_invitees = AsyncMock(return_value=[{"email": "client@acme.com", "name": "Bob Client"}])
        tool = CalendlyGetInviteesTool(client=self.mock_client)
        result = await tool.execute(event_uuid_or_uri="EV-12345")
        self.assertEqual(result["invitees_count"], 1)
        self.assertEqual(result["invitees"][0]["name"], "Bob Client")

    async def test_booking_updates_tool(self) -> None:
        self.mock_client.list_booking_updates = AsyncMock(return_value=[
            {"name": "Product Intro", "status": "active"}
        ])
        tool = CalendlyBookingUpdatesTool(client=self.mock_client)
        result = await tool.execute(max_results=10)
        self.assertEqual(result["updates_count"], 1)
        self.assertEqual(len(result["updates"]), 1)

    def test_get_calendly_tools_returns_all(self) -> None:
        tools = get_calendly_tools("test_token")
        self.assertEqual(len(tools), 6)
        names = {t.name for t in tools}
        self.assertIn("calendly_get_current_user", names)
        self.assertIn("calendly_list_event_types", names)
        self.assertIn("calendly_get_scheduling_link", names)
        self.assertIn("calendly_list_scheduled_events", names)
        self.assertIn("calendly_get_invitees", names)
        self.assertIn("calendly_list_booking_updates", names)


class TestCalendlyTelegramHandlers(unittest.IsolatedAsyncioTestCase):
    """Unit tests for Telegram bot interactions with Calendly."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.encryptor = CredentialEncryptor("unit-test-encryption-key-for-calendly-123")
        self.registry = IntegrationRegistry(encryptor=self.encryptor)

        self.oauth_client = CalendlyOAuthClient(
            client_id="test_client_id",
            client_secret="test_client_secret",
        )
        self.connector = CalendlyConnector(oauth_client=self.oauth_client)
        self.registry.register_connector(self.connector)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def _build_context(self) -> MagicMock:
        context = MagicMock()
        context.user_data = {}
        context.bot_data = {
            "config": MagicMock(
                allowed_telegram_user_ids=frozenset({888}),
                integration_encryption_key="unit-test-encryption-key-for-calendly-123",
                database_url=None,
                calendly_client_id="test_client_id",
                calendly_client_secret="test_client_secret",
            ),
            "integration_registry": self.registry,
        }
        return context

    async def test_connect_calendly_prompt_with_oauth(self) -> None:
        context = self._build_context()
        update = MagicMock(spec=Update)
        update.effective_user = User(id=888, is_bot=False, first_name="Developer")
        update.effective_message = MagicMock(spec=Message)
        update.effective_message.reply_text = AsyncMock()
        update.message = update.effective_message
        context.args = ["calendly"]

        await connect_command(update, context)

        update.effective_message.reply_text.assert_awaited_once()
        sent_text = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Connect Calendly", sent_text)
        self.assertEqual(context.user_data.get("awaiting_calendly_for"), "calendly")

    async def test_handle_calendly_token_submission(self) -> None:
        context = self._build_context()
        context.user_data["awaiting_calendly_for"] = "calendly"

        async with self.session_maker() as session:
            repo = IntegrationRepository(session)
            context.bot_data["integration_repository"] = repo

            update = MagicMock(spec=Update)
            update.update_id = 998811
            user = User(id=888, is_bot=False, first_name="Developer")
            update.effective_user = user
            message = MagicMock(spec=Message)
            message.text = "personal_access_token_xyz"
            message.delete = AsyncMock()
            status_msg = MagicMock()
            status_msg.edit_text = AsyncMock()
            message.reply_text = AsyncMock(return_value=status_msg)
            update.message = message
            update.effective_message = message

            with patch.object(self.connector, "test_connection", new_callable=AsyncMock) as mock_test:
                mock_test.return_value = ConnectionHealth(
                    is_healthy=True,
                    status="healthy",
                    latency_ms=45.0,
                    details={"name": "Alex Founder", "email": "alex@startup.com"},
                )

                await handle_message(update, context)

                # Message containing the token should be immediately deleted
                message.delete.assert_awaited_once()
                # Status message should report successful connection
                status_msg.edit_text.assert_awaited_once()
                final_text = status_msg.edit_text.call_args[0][0]
                self.assertIn("Successfully connected Calendly", final_text)

                # Verify credential was saved and encrypted in database
                record = await repo.get_credentials(888, "calendly")
                self.assertIsNotNone(record)
                decrypted = self.encryptor.decrypt(record.encrypted_credentials)
                self.assertEqual(decrypted["api_key"], "personal_access_token_xyz")

    async def test_cancel_calendly_prompt(self) -> None:
        context = self._build_context()
        context.user_data["awaiting_calendly_for"] = "calendly"

        update = MagicMock(spec=Update)
        update.effective_user = User(id=888, is_bot=False, first_name="Developer")
        message = MagicMock(spec=Message)
        message.reply_text = AsyncMock()
        update.message = message

        await cancel_command(update, context)

        message.reply_text.assert_awaited_once()
        self.assertNotIn("awaiting_calendly_for", context.user_data)
        sent = message.reply_text.call_args[0][0]
        self.assertIn("cancelled", sent.lower())


if __name__ == "__main__":
    unittest.main()
