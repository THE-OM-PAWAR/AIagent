"""Builtin default tools."""

import datetime


def get_current_utc_time() -> str:
    """Return the current ISO formatted UTC time."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

