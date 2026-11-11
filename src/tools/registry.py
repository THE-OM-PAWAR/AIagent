"""Tool registry for registering, discovering, and converting agent tools."""

from collections.abc import Callable
import logging
import threading
from typing import Any

from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission
from src.tools.schemas import ToolDefinition

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Thread-safe registry managing available tools and their metadata."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._callables: dict[str, Callable[..., Any]] = {}
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        description: str,
        func: Callable[..., Any],
        parameters_schema: dict[str, Any] | None = None,
        permission: ToolPermission = ToolPermission.READ_ONLY,
        timeout_seconds: float = 30.0,
        requires_approval: bool = False,
        allowed_agents: list[str] | None = None,
    ) -> None:
        """Register a new callable tool.

        Backward-compatible with original 4-argument signature.
        """
        definition = ToolDefinition(
            name=name,
            description=description,
            parameters_schema=parameters_schema or {"type": "object", "properties": {}},
            permission=permission,
            timeout_seconds=timeout_seconds,
            requires_approval=requires_approval,
            allowed_agents=allowed_agents,
        )
        with self._lock:
            self._definitions[name] = definition
            self._callables[name] = func
        logger.debug("Tool registered: %s (permission=%s)", name, permission.value)

    def register_tool(self, tool: BaseTool) -> None:
        """Register an instance of BaseTool."""
        with self._lock:
            self._definitions[tool.name] = tool.to_definition()
            self._callables[tool.name] = tool.execute
        logger.debug("BaseTool registered: %s (permission=%s)", tool.name, tool.permission.value)

    def get_tool(self, name: str) -> dict[str, Any] | None:
        """Retrieve tool metadata dictionary by name (backward compatible)."""
        with self._lock:
            definition = self._definitions.get(name)
            func = self._callables.get(name)
            if not definition or func is None:
                return None
            return {
                "name": definition.name,
                "description": definition.description,
                "func": func,
                "parameters": definition.parameters_schema,
                "parameters_schema": definition.parameters_schema,
                "permission": definition.permission,
                "timeout_seconds": definition.timeout_seconds,
                "requires_approval": definition.requires_approval,
                "allowed_agents": definition.allowed_agents,
            }

    def get_definition(self, name: str) -> ToolDefinition | None:
        """Retrieve ToolDefinition object by name."""
        with self._lock:
            return self._definitions.get(name)

    def get_callable(self, name: str) -> Callable[..., Any] | None:
        """Retrieve the callable implementation by name."""
        with self._lock:
            return self._callables.get(name)

    def list_tools(self, agent_type: str | None = None) -> list[dict[str, Any]]:
        """Return list of all registered tool descriptions, optionally filtered by agent."""
        with self._lock:
            items: list[dict[str, Any]] = []
            for definition in self._definitions.values():
                if agent_type and definition.allowed_agents:
                    allowed_lower = [a.lower() for a in definition.allowed_agents]
                    if agent_type.lower() not in allowed_lower:
                        continue
                items.append({
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.parameters_schema,
                    "permission": definition.permission.value,
                    "requires_approval": definition.requires_approval,
                })
            return items

    def get_gemini_declarations(self, agent_type: str | None = None) -> list[Any]:
        """Convert registered tools into a list of google.genai.types.FunctionDeclaration."""
        try:
            from google.genai import types
            with self._lock:
                declarations = []
                for definition in self._definitions.values():
                    if agent_type and definition.allowed_agents:
                        allowed_lower = [a.lower() for a in definition.allowed_agents]
                        if agent_type.lower() not in allowed_lower:
                            continue
                    declarations.append(
                        types.FunctionDeclaration(
                            name=definition.name,
                            description=definition.description,
                            parameters_json_schema=definition.parameters_schema or {"type": "object", "properties": {}},
                        )
                    )
                return declarations
        except ImportError:
            with self._lock:
                return [d.to_gemini_declaration_dict() for d in self._definitions.values()]

    def get_gemini_tool(self, agent_type: str | None = None) -> Any | None:
        """Return a google.genai.types.Tool containing all valid function declarations."""
        declarations = self.get_gemini_declarations(agent_type=agent_type)
        if not declarations:
            return None
        try:
            from google.genai import types
            return types.Tool(function_declarations=declarations)
        except ImportError:
            return None

    def register_builtin_tools(
        self,
        memory_manager: Any = None,
        llm_manager: Any = None,
        storage_root: Any = None,
    ) -> None:
        """Convenience method to instantiate and register all built-in P0 tools."""
        from src.tools.builtin.calculator import CalculatorTool
        from src.tools.builtin.datetime_tool import DateTimeTool
        from src.tools.builtin.file_reader import FileReaderTool
        from src.tools.builtin.text_processor import TextProcessorTool
        from src.tools.builtin.web_reader import WebReaderTool

        self.register_tool(CalculatorTool())
        self.register_tool(DateTimeTool())
        self.register_tool(TextProcessorTool(llm_manager=llm_manager))
        self.register_tool(FileReaderTool(storage_root=storage_root or "data/uploads"))
        self.register_tool(WebReaderTool())

        if memory_manager is not None:
            from src.tools.builtin.memory_tools import MemoryOperationsTool
            self.register_tool(MemoryOperationsTool(memory_manager=memory_manager))
        logger.info("Registered built-in tools (calculator, datetime, text_processor, file_reader, web_page_reader%s)",
                    ", memory_operations" if memory_manager else "")
