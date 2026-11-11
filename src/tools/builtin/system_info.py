"""Built-in default system tools."""

import datetime
from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission


def get_current_utc_time() -> str:
    """Return the current ISO formatted UTC time."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class CurrentUtcTimeTool(BaseTool):
    """Tool that returns the current UTC timestamp."""

    @property
    def name(self) -> str:
        return "get_current_utc_time"

    @property
    def description(self) -> str:
        return "Retrieve the current real-world UTC date and time in ISO-8601 format."

    @property
    def parameters_schema(self) -> dict[str, str]:
        return {"type": "object", "properties": {}}

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, **kwargs: object) -> str:
        return get_current_utc_time()
