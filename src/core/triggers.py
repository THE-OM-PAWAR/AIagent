"""Trigger interfaces, recurrence rules, pure-Python cron evaluator, and timezone-aware schedule calculators."""

from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta, timezone
import logging
import re
from typing import Any
import zoneinfo

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_tz(tz_name: str) -> zoneinfo.ZoneInfo:
    """Safely resolve ZoneInfo, falling back to UTC if invalid."""
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except Exception as exc:
        logger.warning("Invalid timezone '%s', defaulting to UTC: %s", tz_name, exc)
        return zoneinfo.ZoneInfo("UTC")


class TriggerValidationError(ValueError):
    """Raised when trigger configuration parameters are invalid."""
    pass


class BaseTrigger(ABC):
    """Abstract base interface for workflow triggers."""

    @property
    @abstractmethod
    def trigger_type(self) -> str:
        """String identifier for trigger type."""
        pass

    @abstractmethod
    def compute_next_run(self, after: datetime, tz_str: str = "UTC") -> datetime | None:
        """Compute the next occurrence strictly after the given timestamp in the specified timezone.

        Returns None if no future execution is scheduled.
        """
        pass

    @abstractmethod
    def validate(self) -> None:
        """Validate configuration parameters."""
        pass


class OneTimeTrigger(BaseTrigger):
    """Triggers exactly once at a specified wall-clock timestamp or immediately."""

    def __init__(self, scheduled_at: datetime | str | None = None, is_immediate: bool = False) -> None:
        self.is_immediate = is_immediate
        if isinstance(scheduled_at, str):
            clean_str = scheduled_at.replace("Z", "+00:00")
            self.scheduled_at = datetime.fromisoformat(clean_str)
        else:
            self.scheduled_at = scheduled_at

    @property
    def trigger_type(self) -> str:
        return "one_time"

    def validate(self) -> None:
        if not self.is_immediate and self.scheduled_at is None:
            raise TriggerValidationError("OneTimeTrigger requires either 'scheduled_at' datetime or is_immediate=True.")

    def compute_next_run(self, after: datetime, tz_str: str = "UTC") -> datetime | None:
        if self.is_immediate:
            return None
        if self.scheduled_at is None:
            return None

        scheduled_utc = _as_utc(self.scheduled_at)
        # If scheduled time has not passed relative to 'after', return it; otherwise, None
        return scheduled_utc if scheduled_utc > _as_utc(after) else None


class IntervalTrigger(BaseTrigger):
    """Triggers repeatedly at a fixed second interval."""

    def __init__(self, interval_seconds: int) -> None:
        self.interval_seconds = int(interval_seconds)
        if self.interval_seconds <= 0:
            raise TriggerValidationError("IntervalTrigger requires interval_seconds > 0.")

    @property
    def trigger_type(self) -> str:
        return "interval"

    def validate(self) -> None:
        if self.interval_seconds < 1:
            raise TriggerValidationError("IntervalTrigger interval_seconds must be at least 1.")

    def compute_next_run(self, after: datetime, tz_str: str = "UTC") -> datetime | None:
        return _as_utc(after) + timedelta(seconds=self.interval_seconds)


class DailyTrigger(BaseTrigger):
    """Triggers once daily at a specific local time in target timezone, handling DST transitions."""

    def __init__(self, time_str: str) -> None:
        self.time_str = time_str.strip()
        parts = self.time_str.split(":")
        if len(parts) != 2:
            raise TriggerValidationError("DailyTrigger time_str must be 'HH:MM' (24-hour format).")
        try:
            self.hour = int(parts[0])
            self.minute = int(parts[1])
        except ValueError:
            raise TriggerValidationError("DailyTrigger time_str must contain integers for 'HH:MM'.")

    @property
    def trigger_type(self) -> str:
        return "daily"

    def validate(self) -> None:
        if not (0 <= self.hour <= 23 and 0 <= self.minute <= 59):
            raise TriggerValidationError("DailyTrigger time must be between 00:00 and 23:59.")

    def compute_next_run(self, after: datetime, tz_str: str = "UTC") -> datetime | None:
        tz = _resolve_tz(tz_str)
        local_after = _as_utc(after).astimezone(tz)

        # Target candidate for today in local time
        candidate = local_after.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if candidate <= local_after:
            # Advance to tomorrow in local calendar
            candidate = candidate + timedelta(days=1)
            # Re-apply timezone to correctly handle DST boundary change
            candidate = datetime(
                candidate.year, candidate.month, candidate.day,
                self.hour, self.minute, 0, 0, tzinfo=tz
            )

        return candidate.astimezone(timezone.utc)


class WeeklyTrigger(BaseTrigger):
    """Triggers on specific days of the week (0=Mon, 6=Sun) at local time in target timezone."""

    _DAY_NAME_MAP = {
        "mon": 0, "monday": 0,
        "tue": 1, "tuesday": 1,
        "wed": 2, "wednesday": 2,
        "thu": 3, "thursday": 3,
        "fri": 4, "friday": 4,
        "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6,
    }

    def __init__(self, time_str: str, days_of_week: list[int | str]) -> None:
        self.time_str = time_str.strip()
        parts = self.time_str.split(":")
        if len(parts) != 2:
            raise TriggerValidationError("WeeklyTrigger time_str must be 'HH:MM' (24-hour format).")
        try:
            self.hour = int(parts[0])
            self.minute = int(parts[1])
        except ValueError:
            raise TriggerValidationError("WeeklyTrigger time_str must contain integers for 'HH:MM'.")

        self.days_of_week: set[int] = set()
        for d in days_of_week:
            if isinstance(d, int) and 0 <= d <= 6:
                self.days_of_week.add(d)
            elif isinstance(d, str):
                normalized = d.strip().lower()
                if normalized in self._DAY_NAME_MAP:
                    self.days_of_week.add(self._DAY_NAME_MAP[normalized])
                elif normalized.isdigit() and 0 <= int(normalized) <= 6:
                    self.days_of_week.add(int(normalized))
                else:
                    raise TriggerValidationError(f"Invalid day of week: '{d}'.")
            else:
                raise TriggerValidationError(f"Invalid day of week value: '{d}'.")

    @property
    def trigger_type(self) -> str:
        return "weekly"

    def validate(self) -> None:
        if not (0 <= self.hour <= 23 and 0 <= self.minute <= 59):
            raise TriggerValidationError("WeeklyTrigger time must be between 00:00 and 23:59.")
        if not self.days_of_week:
            raise TriggerValidationError("WeeklyTrigger requires at least one valid day in days_of_week.")

    def compute_next_run(self, after: datetime, tz_str: str = "UTC") -> datetime | None:
        tz = _resolve_tz(tz_str)
        local_after = _as_utc(after).astimezone(tz)

        # Check today and up to next 7 days
        for day_offset in range(8):
            target_date = (local_after + timedelta(days=day_offset)).date()
            if target_date.weekday() in self.days_of_week:
                candidate = datetime(
                    target_date.year, target_date.month, target_date.day,
                    self.hour, self.minute, 0, 0, tzinfo=tz
                )
                if candidate > local_after:
                    return candidate.astimezone(timezone.utc)

        return None


class CronFieldEvaluator:
    """Parses and matches a single standard cron field (numbers, ranges, steps, commas)."""

    def __init__(self, field_str: str, min_val: int, max_val: int, field_name: str) -> None:
        self.field_str = field_str.strip()
        self.min_val = min_val
        self.max_val = max_val
        self.field_name = field_name
        self.allowed_values = self._parse_field()

    def _parse_field(self) -> set[int]:
        values: set[int] = set()
        subparts = self.field_str.split(",")

        for part in subparts:
            part = part.strip()
            if not part:
                continue

            # Step: */N or range/N
            if "/" in part:
                base, step_str = part.split("/", 1)
                try:
                    step = int(step_str)
                    if step <= 0:
                        raise ValueError()
                except ValueError:
                    raise TriggerValidationError(f"Invalid cron step value in {self.field_name}: '{part}'.")

                if base in ("*", ""):
                    start_val, end_val = self.min_val, self.max_val
                elif "-" in base:
                    r1, r2 = base.split("-", 1)
                    start_val, end_val = int(r1), int(r2)
                else:
                    start_val = int(base)
                    end_val = self.max_val

                values.update(x for x in range(start_val, end_val + 1, step) if self.min_val <= x <= self.max_val)

            elif part == "*":
                values.update(range(self.min_val, self.max_val + 1))

            elif "-" in part:
                r1, r2 = part.split("-", 1)
                try:
                    start_val, end_val = int(r1), int(r2)
                    if start_val > end_val or not (self.min_val <= start_val <= self.max_val and self.min_val <= end_val <= self.max_val):
                        raise ValueError()
                except ValueError:
                    raise TriggerValidationError(f"Invalid cron range in {self.field_name}: '{part}'.")
                values.update(range(start_val, end_val + 1))

            else:
                try:
                    val = int(part)
                    if not (self.min_val <= val <= self.max_val):
                        raise ValueError()
                    values.add(val)
                except ValueError:
                    raise TriggerValidationError(f"Invalid cron value in {self.field_name}: '{part}'.")

        return values

    def matches(self, value: int) -> bool:
        return value in self.allowed_values


class CronTrigger(BaseTrigger):
    """Pure-Python standard 5-field cron evaluator (minute, hour, dom, month, dow)."""

    def __init__(self, cron_expression: str) -> None:
        self.cron_expression = cron_expression.strip()
        parts = self.cron_expression.split()
        if len(parts) != 5:
            raise TriggerValidationError(
                f"Invalid cron expression '{cron_expression}'. Expected 5 fields: 'minute hour dom month dow'."
            )

        self.minute_eval = CronFieldEvaluator(parts[0], 0, 59, "minute")
        self.hour_eval = CronFieldEvaluator(parts[1], 0, 23, "hour")
        self.dom_eval = CronFieldEvaluator(parts[2], 1, 31, "day-of-month")
        self.month_eval = CronFieldEvaluator(parts[3], 1, 12, "month")
        # In cron standard: 0=Sunday or 7=Sunday. Python weekday: 0=Monday, 6=Sunday.
        # We normalize 0-7 where 0 and 7 are Sunday.
        self.dow_eval = CronFieldEvaluator(parts[4], 0, 7, "day-of-week")

    @property
    def trigger_type(self) -> str:
        return "cron"

    def validate(self) -> None:
        pass  # Evaluators validate during initialization

    def _matches(self, dt: datetime) -> bool:
        if not self.minute_eval.matches(dt.minute):
            return False
        if not self.hour_eval.matches(dt.hour):
            return False
        if not self.dom_eval.matches(dt.day):
            return False
        if not self.month_eval.matches(dt.month):
            return False

        # Convert Python weekday (0=Mon..6=Sun) to cron (0=Sun..6=Sat, 7=Sun)
        cron_dow = (dt.weekday() + 1) % 7
        if not (self.dow_eval.matches(cron_dow) or (cron_dow == 0 and self.dow_eval.matches(7))):
            return False

        return True

    def compute_next_run(self, after: datetime, tz_str: str = "UTC") -> datetime | None:
        tz = _resolve_tz(tz_str)
        local_curr = _as_utc(after).astimezone(tz)

        # Advance to the start of the next minute
        local_curr = local_curr.replace(second=0, microsecond=0) + timedelta(minutes=1)

        # Search forward up to 366 days (527,040 minutes max)
        max_minutes = 527040
        for _ in range(max_minutes):
            if self._matches(local_curr):
                return local_curr.astimezone(timezone.utc)
            local_curr += timedelta(minutes=1)

        return None


class EventTrigger(BaseTrigger):
    """Extensible trigger interface for external events or incoming webhooks."""

    def __init__(self, event_name: str, filter_rules: dict[str, Any] | None = None) -> None:
        self.event_name = event_name.strip()
        self.filter_rules = filter_rules or {}

    @property
    def trigger_type(self) -> str:
        return "event"

    def validate(self) -> None:
        if not self.event_name:
            raise TriggerValidationError("EventTrigger requires a non-empty event_name.")

    def compute_next_run(self, after: datetime, tz_str: str = "UTC") -> datetime | None:
        # Event triggers do not have pre-scheduled clock occurrences
        return None


def create_trigger(trigger_type: str, config: dict[str, Any], tz_str: str = "UTC") -> BaseTrigger:
    """Factory creating and validating a BaseTrigger implementation from configuration."""
    tt = trigger_type.lower().strip()

    if tt in ("one_time", "once", "immediate"):
        scheduled_at = config.get("scheduled_at")
        is_immediate = bool(config.get("is_immediate") or tt == "immediate" or not scheduled_at)
        trigger = OneTimeTrigger(scheduled_at=scheduled_at, is_immediate=is_immediate)
    elif tt == "interval":
        interval = config.get("interval_seconds")
        if interval is None:
            # Support helper units e.g. interval_minutes, interval_hours
            if "interval_minutes" in config:
                interval = int(config["interval_minutes"]) * 60
            elif "interval_hours" in config:
                interval = int(config["interval_hours"]) * 3600
            elif "interval_days" in config:
                interval = int(config["interval_days"]) * 86400
            else:
                raise TriggerValidationError("IntervalTrigger config requires 'interval_seconds'.")
        trigger = IntervalTrigger(interval_seconds=int(interval))
    elif tt == "daily":
        time_str = config.get("time") or config.get("time_str")
        if not time_str:
            raise TriggerValidationError("DailyTrigger config requires 'time' ('HH:MM').")
        trigger = DailyTrigger(time_str=str(time_str))
    elif tt == "weekly":
        time_str = config.get("time") or config.get("time_str")
        days = config.get("days_of_week") or config.get("days")
        if not time_str or days is None:
            raise TriggerValidationError("WeeklyTrigger config requires 'time' ('HH:MM') and 'days_of_week'.")
        trigger = WeeklyTrigger(time_str=str(time_str), days_of_week=list(days))
    elif tt == "cron":
        expr = config.get("cron_expression") or config.get("expression") or config.get("cron")
        if not expr:
            raise TriggerValidationError("CronTrigger config requires 'cron_expression'.")
        trigger = CronTrigger(cron_expression=str(expr))
    elif tt == "event":
        event_name = config.get("event_name") or config.get("event")
        if not event_name:
            raise TriggerValidationError("EventTrigger config requires 'event_name'.")
        trigger = EventTrigger(event_name=str(event_name), filter_rules=config.get("filter_rules"))
    else:
        raise TriggerValidationError(f"Unsupported trigger_type '{trigger_type}'.")

    trigger.validate()
    return trigger
