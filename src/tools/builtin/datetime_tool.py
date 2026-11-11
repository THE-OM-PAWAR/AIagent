"""Production-grade timezone-aware date and time calculation tool."""

from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any
import zoneinfo

from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)


def parse_datetime(dt_str: str, default_tz: zoneinfo.ZoneInfo | None = None) -> datetime:
    """Parse an ISO format or common date string into a timezone-aware datetime."""
    clean = dt_str.strip()
    # Try ISO formats
    try:
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=default_tz or timezone.utc)
        return dt
    except ValueError:
        pass

    # Try common formats
    common_formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%Y/%m/%d",
        "%b %d, %Y",
        "%B %d, %Y",
        "%Y-%m-%dT%H:%M:%S%z",
    ]
    for fmt in common_formats:
        try:
            dt = datetime.strptime(clean, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=default_tz or timezone.utc)
            return dt
        except ValueError:
            continue

    raise ValueError(f"Unable to parse date string '{dt_str}'. Please provide ISO-8601 format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS).")


def get_timezone(tz_name: str | None) -> zoneinfo.ZoneInfo:
    """Resolve an IANA timezone identifier safely, defaulting to UTC."""
    if not tz_name or tz_name.upper() in ("UTC", "Z", "GMT"):
        return zoneinfo.ZoneInfo("UTC")
    try:
        return zoneinfo.ZoneInfo(tz_name.strip())
    except Exception as exc:
        raise ValueError(f"Invalid or unrecognized IANA timezone: '{tz_name}'. Examples: 'UTC', 'America/New_York', 'Europe/London', 'Asia/Kolkata', 'Asia/Tokyo'.") from exc


class DateTimeTool(BaseTool):
    """Tool for timezone-aware date/time calculations, conversions, deadlines, and business days."""

    @property
    def name(self) -> str:
        return "datetime_calculator"

    @property
    def description(self) -> str:
        return (
            "Calculate timezone-aware date and time: get current time in any IANA timezone, "
            "convert timezones, compute date differences, add/subtract durations, calculate "
            "deadlines, compare dates, format dates, and calculate business days (excluding weekends)."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Date/time operation to perform.",
                    "enum": [
                        "current_time",
                        "convert_timezone",
                        "date_difference",
                        "add_duration",
                        "calculate_deadline",
                        "compare_dates",
                        "format_date",
                        "business_days",
                    ],
                },
                "timezone": {
                    "type": "string",
                    "description": "Target IANA timezone identifier (e.g. 'UTC', 'America/New_York', 'Asia/Kolkata', 'Europe/Paris'). Default is 'UTC'.",
                },
                "source_timezone": {
                    "type": "string",
                    "description": "Source IANA timezone when converting timezones.",
                },
                "date_string": {
                    "type": "string",
                    "description": "Primary date or timestamp string in ISO-8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS).",
                },
                "target_date_string": {
                    "type": "string",
                    "description": "Secondary or target date string for difference or comparison operations.",
                },
                "days": {"type": "integer", "description": "Number of days to add (positive) or subtract (negative)."},
                "hours": {"type": "integer", "description": "Number of hours to add or subtract."},
                "minutes": {"type": "integer", "description": "Number of minutes to add or subtract."},
                "weeks": {"type": "integer", "description": "Number of weeks to add or subtract."},
                "business_days_count": {
                    "type": "integer",
                    "description": "Number of business days (Monday-Friday) to add or subtract.",
                },
                "format_pattern": {
                    "type": "string",
                    "description": "Custom strftime format pattern or standard keyword ('iso', 'rfc', 'human', 'date_only', 'time_only').",
                },
            },
            "required": ["action"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action", "current_time")
        tz_obj = get_timezone(kwargs.get("timezone"))

        if action in ("current_time", "now"):
            now = datetime.now(tz_obj)
            return {
                "action": "current_time",
                "timezone": tz_obj.key,
                "iso": now.isoformat(),
                "formatted": now.strftime("%A, %B %d, %Y %I:%M:%S %p %Z"),
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                "day_of_week": now.strftime("%A"),
                "is_weekend": now.weekday() >= 5,
            }

        elif action == "convert_timezone":
            dt_str = kwargs.get("date_string")
            if not dt_str:
                now = datetime.now(timezone.utc)
            else:
                source_tz = get_timezone(kwargs.get("source_timezone"))
                now = parse_datetime(dt_str, default_tz=source_tz)

            converted = now.astimezone(tz_obj)
            return {
                "action": "convert_timezone",
                "original": now.isoformat(),
                "original_timezone": str(now.tzinfo),
                "target_timezone": tz_obj.key,
                "converted_iso": converted.isoformat(),
                "formatted": converted.strftime("%A, %B %d, %Y %I:%M:%S %p %Z"),
            }

        elif action == "date_difference":
            d1_str = kwargs.get("date_string")
            d2_str = kwargs.get("target_date_string")
            if not d1_str or not d2_str:
                raise ValueError("Both 'date_string' and 'target_date_string' are required to compute date difference.")

            dt1 = parse_datetime(d1_str, default_tz=tz_obj)
            dt2 = parse_datetime(d2_str, default_tz=tz_obj)

            diff = dt2 - dt1
            total_seconds = int(diff.total_seconds())
            is_future = total_seconds >= 0
            abs_seconds = abs(total_seconds)

            days = abs_seconds // 86400
            rem = abs_seconds % 86400
            hours = rem // 3600
            rem = rem % 3600
            minutes = rem // 60
            seconds = rem % 60

            return {
                "action": "date_difference",
                "from_date": dt1.isoformat(),
                "to_date": dt2.isoformat(),
                "is_positive": is_future,
                "total_seconds": total_seconds,
                "total_days": round(total_seconds / 86400.0, 2),
                "total_weeks": round(total_seconds / (86400.0 * 7), 2),
                "breakdown": {
                    "days": days,
                    "hours": hours,
                    "minutes": minutes,
                    "seconds": seconds,
                },
                "human_summary": f"{days} days, {hours} hours, {minutes} minutes {'later' if is_future else 'earlier'}",
            }

        elif action == "add_duration":
            dt_str = kwargs.get("date_string")
            base_dt = parse_datetime(dt_str, default_tz=tz_obj) if dt_str else datetime.now(tz_obj)

            d = kwargs.get("days", 0)
            h = kwargs.get("hours", 0)
            m = kwargs.get("minutes", 0)
            w = kwargs.get("weeks", 0)
            b_days = kwargs.get("business_days_count")

            if b_days is not None:
                # Add or subtract business days (Monday-Friday)
                curr = base_dt
                step = 1 if b_days >= 0 else -1
                remaining = abs(b_days)
                while remaining > 0:
                    curr += timedelta(days=step)
                    if curr.weekday() < 5:  # 0=Monday, 4=Friday
                        remaining -= 1
                result_dt = curr
            else:
                delta = timedelta(days=d + (w * 7), hours=h, minutes=m)
                result_dt = base_dt + delta

            return {
                "action": "add_duration",
                "base_date": base_dt.isoformat(),
                "result_date": result_dt.isoformat(),
                "formatted": result_dt.strftime("%A, %B %d, %Y %I:%M:%S %p %Z"),
                "day_of_week": result_dt.strftime("%A"),
            }

        elif action == "calculate_deadline":
            deadline_str = kwargs.get("date_string") or kwargs.get("target_date_string")
            if not deadline_str:
                raise ValueError("A deadline 'date_string' must be provided.")

            deadline_dt = parse_datetime(deadline_str, default_tz=tz_obj)
            now = datetime.now(tz_obj)
            diff = deadline_dt - now
            is_passed = diff.total_seconds() < 0

            # Count business days remaining
            start = now.date()
            end = deadline_dt.date()
            if start <= end:
                curr = start
                biz_days = 0
                while curr < end:
                    curr += timedelta(days=1)
                    if curr.weekday() < 5:
                        biz_days += 1
            else:
                curr = end
                biz_days = 0
                while curr < start:
                    curr += timedelta(days=1)
                    if curr.weekday() < 5:
                        biz_days -= 1

            total_days_remaining = round(diff.total_seconds() / 86400.0, 1)

            return {
                "action": "calculate_deadline",
                "current_time": now.isoformat(),
                "deadline": deadline_dt.isoformat(),
                "is_passed": is_passed,
                "calendar_days_remaining": total_days_remaining,
                "business_days_remaining": biz_days,
                "status": "Passed" if is_passed else "Upcoming",
            }

        elif action == "compare_dates":
            d1_str = kwargs.get("date_string")
            d2_str = kwargs.get("target_date_string")
            if not d1_str or not d2_str:
                raise ValueError("Both 'date_string' and 'target_date_string' are required.")

            dt1 = parse_datetime(d1_str, default_tz=tz_obj)
            dt2 = parse_datetime(d2_str, default_tz=tz_obj)

            if dt1 < dt2:
                relation = "earlier"
            elif dt1 > dt2:
                relation = "later"
            else:
                relation = "equal"

            return {
                "action": "compare_dates",
                "date_1": dt1.isoformat(),
                "date_2": dt2.isoformat(),
                "relation": relation,
                "is_same_day": dt1.date() == dt2.date(),
            }

        elif action == "format_date":
            dt_str = kwargs.get("date_string")
            base_dt = parse_datetime(dt_str, default_tz=tz_obj) if dt_str else datetime.now(tz_obj)
            pattern = kwargs.get("format_pattern", "human")

            if pattern == "human":
                formatted = base_dt.strftime("%A, %B %d, %Y at %I:%M %p %Z")
            elif pattern == "iso":
                formatted = base_dt.isoformat()
            elif pattern == "date_only":
                formatted = base_dt.strftime("%Y-%m-%d")
            elif pattern == "time_only":
                formatted = base_dt.strftime("%H:%M:%S")
            elif pattern == "rfc":
                formatted = base_dt.strftime("%a, %d %b %Y %H:%M:%S %z")
            else:
                formatted = base_dt.strftime(pattern)

            return {
                "action": "format_date",
                "input_date": base_dt.isoformat(),
                "pattern": pattern,
                "formatted": formatted,
            }

        elif action == "business_days":
            d1_str = kwargs.get("date_string")
            d2_str = kwargs.get("target_date_string")
            if not d1_str or not d2_str:
                raise ValueError("Both 'date_string' and 'target_date_string' are required.")

            dt1 = parse_datetime(d1_str, default_tz=tz_obj).date()
            dt2 = parse_datetime(d2_str, default_tz=tz_obj).date()

            start_d, end_d = min(dt1, dt2), max(dt1, dt2)
            curr = start_d
            biz_days = 0
            weekend_days = 0

            while curr < end_d:
                curr += timedelta(days=1)
                if curr.weekday() < 5:
                    biz_days += 1
                else:
                    weekend_days += 1

            total_calendar_days = (end_d - start_d).days

            return {
                "action": "business_days",
                "start_date": str(dt1),
                "end_date": str(dt2),
                "total_calendar_days": total_calendar_days,
                "business_days_count": biz_days,
                "weekend_days_count": weekend_days,
            }

        raise ValueError(f"Unknown action '{action}' for datetime_calculator.")

