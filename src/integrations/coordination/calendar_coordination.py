"""Coordination engine synchronizing Calendly bookings with Google Calendar events."""

from datetime import datetime, timezone
import logging
import re
from typing import Any

from src.integrations.connectors.calendar.client import GoogleCalendarClient
from src.integrations.connectors.calendly.client import CalendlyClient
from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)

CALENDLY_TAG_PATTERN = re.compile(r"\[Calendly Booking:\s*([a-zA-Z0-9_-]+)\]", re.IGNORECASE)


def parse_iso_datetime(dt_str: str | None) -> datetime | None:
    """Safely parse an ISO-8601 string into an aware UTC datetime."""
    if not dt_str:
        return None
    try:
        # Standardize trailing Z to +00:00
        clean_str = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class CalendarCoordinator:
    """Coordinates and reconciles scheduled meetings between Calendly and Google Calendar."""

    def __init__(
        self,
        calendly_client: CalendlyClient,
        calendar_client: GoogleCalendarClient,
    ) -> None:
        self.calendly_client = calendly_client
        self.calendar_client = calendar_client

    async def reconcile_events(
        self,
        min_start_time: str | None = None,
        auto_create_missing: bool = True,
        sync_cancellations: bool = True,
    ) -> dict[str, Any]:
        """Reconcile Calendly bookings with Google Calendar events without duplicating entries."""
        now_iso = min_start_time or datetime.now(timezone.utc).isoformat()

        # 1. Retrieve bookings from Calendly
        active_bookings = await self.calendly_client.list_scheduled_events(status="active", min_start_time=now_iso)
        canceled_bookings = await self.calendly_client.list_scheduled_events(status="canceled", min_start_time=now_iso)

        # 2. Retrieve existing events from Google Calendar
        calendar_events = await self.calendar_client.list_events(time_min=now_iso, max_results=50)

        # Build index of Google Calendar events by Calendly Tag UUID and by (start_time_utc, summary)
        events_by_calendly_uuid: dict[str, dict[str, Any]] = {}
        events_by_time_slot: dict[str, dict[str, Any]] = {}

        for ev in calendar_events:
            desc = ev.get("description", "") or ""
            match = CALENDLY_TAG_PATTERN.search(desc)
            if match:
                uuid = match.group(1).strip()
                events_by_calendly_uuid[uuid] = ev

            start_dt = parse_iso_datetime(ev.get("start"))
            if start_dt:
                slot_key = f"{start_dt.strftime('%Y-%m-%dT%H:%M')}_{ev.get('summary', '').lower().strip()}"
                events_by_time_slot[slot_key] = ev

        created_count = 0
        updated_count = 0
        matched_count = 0
        cancellations_count = 0
        reconciliation_log = []

        # 3. Process Active Calendly Bookings
        for booking in active_bookings:
            b_uuid = booking.get("event_uuid") or booking.get("uri", "").rstrip("/").split("/")[-1]
            b_name = booking.get("name", "Meeting")
            b_start_str = booking.get("start_time")
            b_end_str = booking.get("end_time")

            b_start_dt = parse_iso_datetime(b_start_str)
            if not b_start_dt:
                continue

            # Check if event is already mapped in Google Calendar
            existing_event = events_by_calendly_uuid.get(b_uuid)

            # Fallback heuristic: check time slot and summary similarity to prevent duplicates
            if not existing_event:
                slot_key = f"{b_start_dt.strftime('%Y-%m-%dT%H:%M')}_{b_name.lower().strip()}"
                existing_event = events_by_time_slot.get(slot_key)

            if existing_event:
                matched_count += 1
                reconciliation_log.append({
                    "action": "matched",
                    "calendly_uuid": b_uuid,
                    "calendar_event_id": existing_event.get("id"),
                    "summary": existing_event.get("summary"),
                })
            elif auto_create_missing:
                # Retrieve invitee info for comprehensive calendar event creation
                invitees = []
                try:
                    invitees = await self.calendly_client.get_invitees(booking.get("uri", b_uuid))
                except Exception as inv_err:
                    logger.debug("Failed retrieving invitee details for %s: %s", b_uuid, inv_err)

                primary_invitee = invitees[0] if invitees else {}
                inv_name = primary_invitee.get("name") or "Invitee"
                inv_email = primary_invitee.get("email") or ""

                event_title = f"Calendly: {b_name} - {inv_name}"
                event_desc = (
                    f"📅 Calendly Booking\n"
                    f"Event Type: {b_name}\n"
                    f"Invitee: {inv_name} ({inv_email})\n\n"
                    f"[Calendly Booking: {b_uuid}]"
                )
                attendees = [inv_email] if inv_email else None

                try:
                    created = await self.calendar_client.create_event(
                        summary=event_title,
                        start_time=b_start_str,
                        end_time=b_end_str,
                        description=event_desc,
                        attendees=attendees,
                    )
                    created_count += 1
                    reconciliation_log.append({
                        "action": "created",
                        "calendly_uuid": b_uuid,
                        "calendar_event_id": created.get("id"),
                        "summary": event_title,
                        "start_time": b_start_str,
                    })
                except Exception as create_err:
                    logger.warning("Failed creating synchronized calendar event for %s: %s", b_uuid, create_err)

        # 4. Process Canceled Calendly Bookings
        if sync_cancellations:
            for canceled in canceled_bookings:
                c_uuid = canceled.get("event_uuid") or canceled.get("uri", "").rstrip("/").split("/")[-1]
                existing_event = events_by_calendly_uuid.get(c_uuid)

                if existing_event:
                    summary = existing_event.get("summary", "")
                    if not summary.startswith("[CANCELED]"):
                        new_summary = f"[CANCELED] {summary}"
                        try:
                            await self.calendar_client.update_event(
                                event_id=existing_event["id"],
                                summary=new_summary,
                                description=f"⚠️ This Calendly booking was canceled.\n\n{existing_event.get('description', '')}",
                            )
                            cancellations_count += 1
                            reconciliation_log.append({
                                "action": "canceled_sync",
                                "calendly_uuid": c_uuid,
                                "calendar_event_id": existing_event["id"],
                                "updated_summary": new_summary,
                            })
                        except Exception as cancel_err:
                            logger.warning("Failed updating canceled event %s: %s", existing_event["id"], cancel_err)

        return {
            "status": "success",
            "calendly_active_count": len(active_bookings),
            "calendly_canceled_count": len(canceled_bookings),
            "google_events_examined": len(calendar_events),
            "events_matched": matched_count,
            "events_created": created_count,
            "events_canceled": cancellations_count,
            "reconciliation_log": reconciliation_log,
        }


class CalendarCalendlyCoordinationTool(BaseTool):
    """Tool to synchronize and coordinate Calendly bookings with Google Calendar."""

    def __init__(
        self,
        calendly_client: CalendlyClient | None = None,
        calendar_client: GoogleCalendarClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._calendly_client = calendly_client
        self._calendar_client = calendar_client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "coordination_sync_calendly_calendar"

    @property
    def description(self) -> str:
        return (
            "Coordinate Calendly bookings with Google Calendar: retrieves upcoming reservations, "
            "normalizes timezones, avoids duplicate entries, creates missing events, and synchronizes cancellations."
        )

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "min_start_time": {
                    "type": "string",
                    "description": "Optional ISO-8601 start datetime filter (defaults to current time).",
                },
                "auto_create_missing": {
                    "type": "boolean",
                    "description": "Whether to automatically create calendar entries for unsynced Calendly bookings (default: True).",
                    "default": True,
                },
                "sync_cancellations": {
                    "type": "boolean",
                    "description": "Whether to tag or update canceled Calendly bookings in Google Calendar (default: True).",
                    "default": True,
                },
            },
        }

    async def execute(
        self,
        min_start_time: str | None = None,
        auto_create_missing: bool = True,
        sync_cancellations: bool = True,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_calendly = self._calendly_client
        active_calendar = self._calendar_client

        if user_id and self._integration_registry and self._database_url:
            try:
                from src.database.repositories.integration_repo import IntegrationRepository
                from src.database.session import get_async_session

                async with get_async_session(self._database_url) as session:
                    repo = IntegrationRepository(session)
                    c_creds = await self._integration_registry.get_credentials(repo, user_id, "calendly")
                    if c_creds:
                        t = c_creds.get("access_token") or c_creds.get("api_key")
                        if t:
                            active_calendly = CalendlyClient(token=t)

                    g_creds = await self._integration_registry.get_credentials(repo, user_id, "google_calendar")
                    if not g_creds:
                        g_creds = await self._integration_registry.get_credentials(repo, user_id, "google")
                    if g_creds and g_creds.get("access_token"):
                        active_calendar = GoogleCalendarClient(access_token=g_creds["access_token"])
            except Exception as exc:
                logger.debug("Failed resolving user credentials for coordination: %s", exc)

        if not active_calendly or not active_calendly.token:
            return {"error": "Calendly is not connected. Use /connect calendly in Telegram."}
        if not active_calendar or not active_calendar.access_token:
            return {"error": "Google Calendar is not connected. Use /connect google in Telegram."}

        coordinator = CalendarCoordinator(
            calendly_client=active_calendly,
            calendar_client=active_calendar,
        )
        return await coordinator.reconcile_events(
            min_start_time=min_start_time,
            auto_create_missing=auto_create_missing,
            sync_cancellations=sync_cancellations,
        )

