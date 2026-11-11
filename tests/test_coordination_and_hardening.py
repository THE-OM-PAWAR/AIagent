"""Comprehensive tests for Calendar-Calendly Coordination, Resilient HTTP Client, and 5 End-to-End Workflows."""

import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.integrations.connectors.calendar.client import GoogleCalendarClient
from src.integrations.connectors.calendly.client import CalendlyClient
from src.integrations.connectors.drive.client import GoogleDriveClient
from src.integrations.connectors.gmail.client import GmailClient
from src.integrations.connectors.maps.client import GooglePlacesClient
from src.integrations.coordination.calendar_coordination import (
    CALENDLY_TAG_PATTERN,
    CalendarCalendlyCoordinationTool,
    CalendarCoordinator,
    parse_iso_datetime,
)
from src.integrations.errors import (
    AuthenticationError,
    ProviderUnavailableError,
    RateLimitError,
)
from src.integrations.http_client import (
    IntegrationHttpClient,
    mask_headers_for_logging,
    parse_retry_after,
)
from src.tools.permissions import ToolPermission


class TestCalendarCoordination(unittest.IsolatedAsyncioTestCase):
    """Unit tests for CalendarCoordinator synchronization and deduplication logic."""

    def setUp(self) -> None:
        self.mock_calendly = MagicMock(spec=CalendlyClient)
        self.mock_calendly.token = "cal_token_123"
        self.mock_calendar = MagicMock(spec=GoogleCalendarClient)
        self.mock_calendar.access_token = "goog_token_123"

        self.coordinator = CalendarCoordinator(
            calendly_client=self.mock_calendly,
            calendar_client=self.mock_calendar,
        )

    def test_parse_iso_datetime(self) -> None:
        # UTC Z
        dt_utc = parse_iso_datetime("2026-11-20T14:30:00Z")
        self.assertIsNotNone(dt_utc)
        self.assertEqual(dt_utc.tzinfo, timezone.utc)
        self.assertEqual(dt_utc.hour, 14)
        self.assertEqual(dt_utc.minute, 30)

        # Non-UTC positive offset (+05:30)
        dt_ist = parse_iso_datetime("2026-11-20T20:00:00+05:30")
        self.assertIsNotNone(dt_ist)
        self.assertEqual(dt_ist.tzinfo, timezone.utc)
        self.assertEqual(dt_ist.hour, 14)
        self.assertEqual(dt_ist.minute, 30)

        # Non-UTC negative offset (-05:00)
        dt_est = parse_iso_datetime("2026-11-20T09:30:00-05:00")
        self.assertIsNotNone(dt_est)
        self.assertEqual(dt_est.tzinfo, timezone.utc)
        self.assertEqual(dt_est.hour, 14)
        self.assertEqual(dt_est.minute, 30)

        # Naive datetime
        dt_naive = parse_iso_datetime("2026-11-20T14:30:00")
        self.assertIsNotNone(dt_naive)
        self.assertEqual(dt_naive.tzinfo, timezone.utc)

        # Invalid string
        self.assertIsNone(parse_iso_datetime("invalid-date-string"))
        self.assertIsNone(parse_iso_datetime(None))

    def test_calendly_tag_regex(self) -> None:
        text = "Meeting with VIP.\n\n[Calendly Booking: EVT-9988-ABC]\nThanks!"
        match = CALENDLY_TAG_PATTERN.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "EVT-9988-ABC")

    async def test_reconcile_creates_missing_event(self) -> None:
        # Calendly has 1 active booking
        self.mock_calendly.list_scheduled_events = AsyncMock(side_effect=[
            [
                {
                    "uri": "https://api.calendly.com/scheduled_events/EV-NEW-1",
                    "event_uuid": "EV-NEW-1",
                    "name": "Strategy Session",
                    "status": "active",
                    "start_time": "2026-11-25T16:00:00Z",
                    "end_time": "2026-11-25T16:45:00Z",
                }
            ],
            [],  # canceled
        ])
        self.mock_calendly.get_invitees = AsyncMock(return_value=[
            {"name": "Alice Partner", "email": "alice@partner.com"}
        ])

        # Google Calendar has no events
        self.mock_calendar.list_events = AsyncMock(return_value=[])
        self.mock_calendar.create_event = AsyncMock(return_value={"id": "gcal_evt_123"})

        result = await self.coordinator.reconcile_events(
            auto_create_missing=True,
            sync_cancellations=True,
        )

        self.assertEqual(result["events_created"], 1)
        self.assertEqual(result["events_matched"], 0)
        self.assertEqual(result["events_canceled"], 0)

        self.mock_calendar.create_event.assert_awaited_once()
        create_kwargs = self.mock_calendar.create_event.call_args[1]
        self.assertIn("Strategy Session", create_kwargs["summary"])
        self.assertIn("[Calendly Booking: EV-NEW-1]", create_kwargs["description"])
        self.assertEqual(create_kwargs["attendees"], ["alice@partner.com"])

    async def test_reconcile_avoids_duplicate_with_tag(self) -> None:
        # Calendly active booking
        self.mock_calendly.list_scheduled_events = AsyncMock(side_effect=[
            [
                {
                    "uri": "https://api.calendly.com/scheduled_events/EV-EXISTS-1",
                    "event_uuid": "EV-EXISTS-1",
                    "name": "Tech Architecture Review",
                    "status": "active",
                    "start_time": "2026-11-25T18:00:00Z",
                    "end_time": "2026-11-25T18:30:00Z",
                }
            ],
            [],  # canceled
        ])

        # Google Calendar already has event with [Calendly Booking: EV-EXISTS-1]
        self.mock_calendar.list_events = AsyncMock(return_value=[
            {
                "id": "gcal_existing_999",
                "summary": "Calendly: Tech Architecture Review",
                "start": "2026-11-25T18:00:00Z",
                "description": "Details here\n\n[Calendly Booking: EV-EXISTS-1]",
            }
        ])
        self.mock_calendar.create_event = AsyncMock()

        result = await self.coordinator.reconcile_events(
            auto_create_missing=True,
            sync_cancellations=True,
        )

        self.assertEqual(result["events_created"], 0)
        self.assertEqual(result["events_matched"], 1)
        self.mock_calendar.create_event.assert_not_awaited()

    async def test_reconcile_avoids_duplicate_via_time_slot_heuristic(self) -> None:
        # Calendly active booking
        self.mock_calendly.list_scheduled_events = AsyncMock(side_effect=[
            [
                {
                    "uri": "https://api.calendly.com/scheduled_events/EV-TIME-MATCH",
                    "event_uuid": "EV-TIME-MATCH",
                    "name": "Sprint Planning",
                    "status": "active",
                    "start_time": "2026-11-25T10:00:00Z",
                    "end_time": "2026-11-25T11:00:00Z",
                }
            ],
            [],
        ])

        # Google Calendar has an event at the exact same start time and matching summary
        self.mock_calendar.list_events = AsyncMock(return_value=[
            {
                "id": "gcal_slot_555",
                "summary": "Sprint Planning",
                "start": "2026-11-25T10:00:00Z",
                "description": "Team sprint planning meeting",
            }
        ])
        self.mock_calendar.create_event = AsyncMock()

        result = await self.coordinator.reconcile_events(
            auto_create_missing=True,
            sync_cancellations=True,
        )

        self.assertEqual(result["events_created"], 0)
        self.assertEqual(result["events_matched"], 1)
        self.mock_calendar.create_event.assert_not_awaited()

    async def test_reconcile_syncs_cancellations(self) -> None:
        # Active bookings empty, canceled bookings has 1
        self.mock_calendly.list_scheduled_events = AsyncMock(side_effect=[
            [],  # active
            [
                {
                    "uri": "https://api.calendly.com/scheduled_events/EV-CANCELLED",
                    "event_uuid": "EV-CANCELLED",
                    "name": "Introductory Call",
                    "status": "canceled",
                    "start_time": "2026-11-26T14:00:00Z",
                }
            ],
        ])

        # Google Calendar has the existing event
        self.mock_calendar.list_events = AsyncMock(return_value=[
            {
                "id": "gcal_evt_to_cancel",
                "summary": "Calendly: Introductory Call",
                "start": "2026-11-26T14:00:00Z",
                "description": "Original call info\n\n[Calendly Booking: EV-CANCELLED]",
            }
        ])
        self.mock_calendar.update_event = AsyncMock(return_value={"id": "gcal_evt_to_cancel"})

        result = await self.coordinator.reconcile_events(
            auto_create_missing=True,
            sync_cancellations=True,
        )

        self.assertEqual(result["events_canceled"], 1)
        self.mock_calendar.update_event.assert_awaited_once()
        update_kwargs = self.mock_calendar.update_event.call_args[1]
        self.assertTrue(update_kwargs["summary"].startswith("[CANCELED]"))
        self.assertIn("This Calendly booking was canceled", update_kwargs["description"])


class TestCalendarCalendlyCoordinationTool(unittest.IsolatedAsyncioTestCase):
    """Unit tests for CalendarCalendlyCoordinationTool."""

    def test_tool_metadata(self) -> None:
        tool = CalendarCalendlyCoordinationTool()
        self.assertEqual(tool.name, "coordination_sync_calendly_calendar")
        self.assertEqual(tool.permission, ToolPermission.WRITE)
        self.assertIn("min_start_time", tool.parameters_schema["properties"])

    async def test_execute_unconnected_returns_error(self) -> None:
        tool = CalendarCalendlyCoordinationTool()
        result = await tool.execute()
        self.assertIn("error", result)


class TestIntegrationHttpClient(unittest.IsolatedAsyncioTestCase):
    """Unit tests for resilient IntegrationHttpClient with retries, backoff, and idempotency."""

    def test_mask_headers_for_logging(self) -> None:
        headers = {
            "Authorization": "Bearer super-secret-key-12345",
            "Content-Type": "application/json",
            "X-Api-Key": "my-api-key",
            "User-Agent": "AIAgent/1.0",
        }
        masked = mask_headers_for_logging(headers)
        self.assertEqual(masked["Authorization"], "[REDACTED]")
        self.assertEqual(masked["X-Api-Key"], "[REDACTED]")
        self.assertEqual(masked["Content-Type"], "application/json")
        self.assertEqual(masked["User-Agent"], "AIAgent/1.0")

    def test_parse_retry_after(self) -> None:
        # Delta seconds
        self.assertEqual(parse_retry_after("15"), 15.0)
        self.assertEqual(parse_retry_after("0.5"), 0.5)

        # None or empty
        self.assertEqual(parse_retry_after(None, default_delay=3.0), 3.0)
        self.assertEqual(parse_retry_after("", default_delay=2.0), 2.0)

    @patch("httpx.AsyncClient.send")
    async def test_successful_request_and_telemetry(self, mock_send: AsyncMock) -> None:
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_send.return_value = mock_response

        client = IntegrationHttpClient(provider_id="test_service")
        resp = await client.get("https://api.example.com/data")
        self.assertEqual(resp.status_code, 200)

        telemetry = client.get_telemetry()
        self.assertEqual(telemetry["provider_id"], "test_service")
        self.assertEqual(telemetry["total_requests"], 1)
        self.assertEqual(telemetry["successful_requests"], 1)
        self.assertEqual(telemetry["failed_requests"], 0)
        self.assertEqual(telemetry["retries_performed"], 0)
        self.assertGreaterEqual(telemetry["average_latency_seconds"], 0.0)

    @patch("httpx.AsyncClient.send")
    async def test_retry_on_500_with_backoff(self, mock_send: AsyncMock) -> None:
        # First attempt returns 500, second returns 200
        err_response = MagicMock(spec=httpx.Response)
        err_response.status_code = 500
        err_response.is_error = True
        err_response.text = "Internal Server Error"

        success_response = MagicMock(spec=httpx.Response)
        success_response.status_code = 200
        success_response.is_error = False

        mock_send.side_effect = [err_response, success_response]

        client = IntegrationHttpClient(
            provider_id="flaky_service",
            base_backoff_seconds=0.01,
            max_backoff_seconds=0.05,
        )

        resp = await client.get("https://api.example.com/resource")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_send.call_count, 2)

        telemetry = client.get_telemetry()
        self.assertEqual(telemetry["total_requests"], 1)
        self.assertEqual(telemetry["successful_requests"], 1)
        self.assertEqual(telemetry["retries_performed"], 1)

    @patch("httpx.AsyncClient.send")
    async def test_rate_limit_429_respects_retry_after(self, mock_send: AsyncMock) -> None:
        # First attempt returns 429 with Retry-After: 0.05, second returns 200
        rate_limit_resp = MagicMock(spec=httpx.Response)
        rate_limit_resp.status_code = 429
        rate_limit_resp.is_error = True
        rate_limit_resp.headers = {"Retry-After": "0.05"}
        rate_limit_resp.text = "Rate limit exceeded"

        success_resp = MagicMock(spec=httpx.Response)
        success_resp.status_code = 200
        success_resp.is_error = False

        mock_send.side_effect = [rate_limit_resp, success_resp]

        client = IntegrationHttpClient(
            provider_id="rate_limited_service",
            base_backoff_seconds=0.01,
        )

        resp = await client.get("https://api.example.com/metered")
        self.assertEqual(resp.status_code, 200)

        telemetry = client.get_telemetry()
        self.assertEqual(telemetry["rate_limit_events"], 1)
        self.assertEqual(telemetry["retries_performed"], 1)

    @patch("httpx.AsyncClient.send")
    async def test_bounded_retries_exhaustion_raises(self, mock_send: AsyncMock) -> None:
        # Consistently fails with 503
        err_response = MagicMock(spec=httpx.Response)
        err_response.status_code = 503
        err_response.is_error = True
        err_response.text = "Service Unavailable"
        mock_send.return_value = err_response

        client = IntegrationHttpClient(
            provider_id="down_service",
            max_retries=2,
            base_backoff_seconds=0.01,
            max_backoff_seconds=0.02,
        )

        with self.assertRaises(ProviderUnavailableError):
            await client.get("https://api.example.com/status")

        telemetry = client.get_telemetry()
        self.assertEqual(telemetry["failed_requests"], 1)
        self.assertEqual(telemetry["retries_performed"], 2)

    @patch("httpx.AsyncClient.send")
    async def test_non_idempotent_post_not_retried_on_timeout(self, mock_send: AsyncMock) -> None:
        # POST timeout without explicit idempotent=True must not retry to avoid double creation
        mock_send.side_effect = httpx.ReadTimeout("Read timed out on POST /charge")

        client = IntegrationHttpClient(
            provider_id="payment_service",
            max_retries=3,
        )

        with self.assertRaises(ProviderUnavailableError) as ctx:
            await client.post("https://api.example.com/charge", json={"amount": 100}, idempotent=False)

        self.assertIn("non-idempotent", str(ctx.exception).lower())
        # Should only have attempted once
        self.assertEqual(mock_send.call_count, 1)


class TestEndToEndWorkflows(unittest.IsolatedAsyncioTestCase):
    """Comprehensive verification of the 5 cross-integration user workflows."""

    @patch("src.integrations.http_client.IntegrationHttpClient.get")
    async def test_workflow_1_business_discovery(self, mock_get: AsyncMock) -> None:
        """Workflow 1: Places search -> place details -> lead summary."""
        places_client = GooglePlacesClient(api_key="mock_places_key")

        # Mock Places search followed by Places details
        mock_get.side_effect = [
            # Search response
            MagicMock(
                status_code=200,
                json=lambda: {
                    "results": [
                        {
                            "place_id": "ChIJ_TEST_COFFEE",
                            "name": "Artisan Coffee Roasters",
                            "formatted_address": "123 Market St, San Francisco, CA",
                            "rating": 4.8,
                            "user_ratings_total": 450,
                        }
                    ],
                    "status": "OK",
                },
            ),
            # Details response
            MagicMock(
                status_code=200,
                json=lambda: {
                    "result": {
                        "name": "Artisan Coffee Roasters",
                        "formatted_phone_number": "(415) 555-0199",
                        "website": "https://artisancoffee.sf",
                        "formatted_address": "123 Market St, San Francisco, CA",
                        "rating": 4.8,
                        "business_status": "OPERATIONAL",
                    },
                    "status": "OK",
                },
            ),
        ]

        # 1. Search
        search_results = await places_client.text_search("coffee shops in SF")
        self.assertEqual(len(search_results), 1)
        lead_place_id = search_results[0]["place_id"]

        # 2. Get details
        lead_details = await places_client.get_place_details(lead_place_id)
        self.assertEqual(lead_details["name"], "Artisan Coffee Roasters")
        self.assertEqual(lead_details["phone_number"], "(415) 555-0199")
        self.assertEqual(lead_details["website"], "https://artisancoffee.sf")

    @patch("src.integrations.http_client.IntegrationHttpClient.post")
    async def test_workflow_2_outreach_execution(self, mock_post: AsyncMock) -> None:
        """Workflow 2: Draft generation -> approval verification -> send email."""
        gmail_client = GmailClient(access_token="mock_gmail_token")

        mock_post.side_effect = [
            # Draft creation response
            MagicMock(
                status_code=200,
                json=lambda: {
                    "id": "draft_abc_123",
                    "message": {"id": "msg_xyz_456"},
                },
            ),
            # Send message response
            MagicMock(
                status_code=200,
                json=lambda: {
                    "id": "msg_sent_789",
                    "threadId": "thread_sent_789",
                },
            ),
        ]

        # 1. Create outreach draft
        draft = await gmail_client.create_draft(
            to="partner@targetcompany.com",
            subject="Partnership Discussion",
            body="Hello,\nWe would love to discuss a collaboration.\nBest regards,\nAlex",
        )
        self.assertEqual(draft["draft_id"], "draft_abc_123")

        # 2. Simulate user approval gate and final send
        sent_message = await gmail_client.send_email(
            to="partner@targetcompany.com",
            subject="Partnership Discussion",
            body="Hello,\nWe would love to discuss a collaboration.\nBest regards,\nAlex",
        )
        self.assertEqual(sent_message["message_id"], "msg_sent_789")

    @patch("src.integrations.http_client.IntegrationHttpClient.get")
    @patch("src.integrations.http_client.IntegrationHttpClient.post")
    async def test_workflow_3_meeting_management(self, mock_post: AsyncMock, mock_get: AsyncMock) -> None:
        """Workflow 3: Calendly booking fetch -> Calendar coordinator sync -> verify event creation."""
        calendly_client = CalendlyClient(token="cal_token")
        calendar_client = GoogleCalendarClient(access_token="gcal_token")

        coordinator = CalendarCoordinator(
            calendly_client=calendly_client,
            calendar_client=calendar_client,
        )

        # Mock Calendly list active bookings & invitees
        calendly_client.list_scheduled_events = AsyncMock(side_effect=[
            [
                {
                    "uri": "https://api.calendly.com/scheduled_events/EV-WORKFLOW-3",
                    "event_uuid": "EV-WORKFLOW-3",
                    "name": "Investor Pitch",
                    "status": "active",
                    "start_time": "2026-11-28T14:00:00Z",
                    "end_time": "2026-11-28T14:30:00Z",
                }
            ],
            [],
        ])
        calendly_client.get_invitees = AsyncMock(return_value=[
            {"name": "Venture Partner", "email": "partner@vc.com"}
        ])

        # Mock Google Calendar listing (empty) and event creation
        calendar_client.list_events = AsyncMock(return_value=[])
        calendar_client.create_event = AsyncMock(return_value={"id": "gcal_evt_pitch_1"})

        result = await coordinator.reconcile_events()
        self.assertEqual(result["events_created"], 1)
        self.assertEqual(result["events_matched"], 0)
        calendar_client.create_event.assert_awaited_once()

    @patch("src.integrations.http_client.IntegrationHttpClient.get")
    @patch("src.integrations.http_client.IntegrationHttpClient.post")
    async def test_workflow_4_meeting_scheduling(self, mock_post: AsyncMock, mock_get: AsyncMock) -> None:
        """Workflow 4: Calendly event types -> single-use link generation."""
        calendly_client = CalendlyClient(token="cal_token")

        # Mock list event types
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: {"resource": {"uri": "https://api.calendly.com/users/ME"}}),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "collection": [
                        {
                            "uri": "https://api.calendly.com/event_types/ET_DEMO",
                            "name": "30 Min Product Demo",
                            "active": True,
                            "scheduling_url": "https://calendly.com/demo",
                            "duration": 30,
                        }
                    ]
                },
            ),
        ]

        # Mock scheduling link generation
        mock_post.return_value = MagicMock(
            status_code=201,
            json=lambda: {
                "resource": {
                    "booking_url": "https://calendly.com/d/custom-single-use-url",
                    "owner": "https://api.calendly.com/event_types/ET_DEMO",
                    "owner_type": "EventType",
                }
            },
        )

        event_types = await calendly_client.list_event_types()
        self.assertEqual(len(event_types), 1)
        et_uri = event_types[0]["uri"]

        link_info = await calendly_client.get_scheduling_link(et_uri, max_event_count=1)
        self.assertEqual(link_info["booking_url"], "https://calendly.com/d/custom-single-use-url")

    @patch("src.integrations.http_client.IntegrationHttpClient.get")
    async def test_workflow_5_document_access(self, mock_get: AsyncMock) -> None:
        """Workflow 5: Google Drive search for contract/doc -> read document content."""
        drive_client = GoogleDriveClient(access_token="mock_drive_token")

        # Mock search files followed by file metadata and export content
        mock_get.side_effect = [
            # 1. Search response
            MagicMock(
                status_code=200,
                json=lambda: {
                    "files": [
                        {
                            "id": "doc_file_abc",
                            "name": "Partnership_Agreement_2026",
                            "mimeType": "application/vnd.google-apps.document",
                            "size": "45000",
                        }
                    ]
                },
            ),
            # 2. File metadata response for get_file_metadata
            MagicMock(
                status_code=200,
                json=lambda: {
                    "id": "doc_file_abc",
                    "name": "Partnership_Agreement_2026",
                    "mimeType": "application/vnd.google-apps.document",
                    "size": "45000",
                },
            ),
            # 3. Document plain text export response
            MagicMock(
                status_code=200,
                text="PARTNERSHIP AGREEMENT\n\nThis agreement is made between Company A and Company B.",
            ),
        ]

        # 1. Search for document
        files = await drive_client.search_files(query="name contains 'Partnership_Agreement'")
        self.assertEqual(len(files), 1)
        doc_id = files[0]["id"]

        # 2. Read document text content
        doc_data = await drive_client.read_document(doc_id)
        self.assertIn("PARTNERSHIP AGREEMENT", doc_data["content"])
        self.assertIn("Company A and Company B", doc_data["content"])


if __name__ == "__main__":
    unittest.main()
