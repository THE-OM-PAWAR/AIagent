"""Safe executor for invoking tools with parameter handling and timeouts."""

import asyncio
from inspect import iscoroutinefunction
import logging
from typing import Any

from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Executes tools safely with timeouts and structured error capture."""

    def __init__(self, registry: ToolRegistry, default_timeout: float = 30.0) -> None:
        self.registry = registry
        self.default_timeout = default_timeout

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool by name with arguments.

        Args:
            tool_name: Name of registered tool.
            arguments: Dictionary of arguments.

        Returns:
            Dict containing 'success', 'result', or 'error'.
        """
        tool = self.registry.get_tool(tool_name)
        if not tool:
            return {"success": False, "error": f"Tool '{tool_name}' not found."}

        func = tool["func"]
        try:
            if iscoroutinefunction(func):
                result = await asyncio.wait_for(
                    func(**arguments),
                    timeout=self.default_timeout,
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(func, **arguments),
                    timeout=self.default_timeout,
                )
            return {"success": True, "result": result}
        except asyncio.TimeoutError:
            logger.warning("Tool '%s' timed out after %.1fs", tool_name, self.default_timeout)
            return {"success": False, "error": f"Tool execution timed out."}
        except Exception as exc:
            logger.error("Error executing tool '%s': %s", tool_name, exc, exc_info=True)
            return {"success": False, "error": str(exc)}

