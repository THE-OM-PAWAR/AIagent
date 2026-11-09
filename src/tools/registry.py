"""Tool registry for registering and discovering agent tools."""

from collections.abc import Callable
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Manages available tools that the assistant can execute."""

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        func: Callable[..., Any],
        parameters_schema: dict[str, Any] | None = None,
    ) -> None:
        """Register a new tool.

        Args:
            name: Unique name of the tool.
            description: Tool description explaining its capability.
            func: Callable implementation of the tool.
            parameters_schema: Optional JSON-schema describing input arguments.
        """
        self._tools[name] = {
            "name": name,
            "description": description,
            "func": func,
            "parameters": parameters_schema or {},
        }
        logger.debug("Tool registered: %s", name)

    def get_tool(self, name: str) -> dict[str, Any] | None:
        """Retrieve tool metadata and function by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        """Return list of all registered tool descriptions."""
        return [
            {
                "name": meta["name"],
                "description": meta["description"],
                "parameters": meta["parameters"],
            }
            for meta in self._tools.values()
        ]

