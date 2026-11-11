"""Base class abstractions for executable tools."""

from abc import ABC, abstractmethod
import logging
from typing import Any

from src.tools.permissions import ToolPermission
from src.tools.schemas import ToolDefinition

logger = logging.getLogger(__name__)


class BaseTool(ABC):
    """Abstract base class for all tools in the AIagent tool ecosystem."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier name for the tool (e.g. 'calculator')."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Detailed description of the tool capability for LLM prompt context."""
        pass

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """JSON schema defining the accepted input arguments."""
        return {"type": "object", "properties": {}}

    @property
    def permission(self) -> ToolPermission:
        """Access permission tier required to execute this tool."""
        return ToolPermission.READ_ONLY

    @property
    def timeout_seconds(self) -> float:
        """Maximum allowed execution duration before timing out."""
        return 30.0

    @property
    def requires_approval(self) -> bool:
        """Whether executing this tool requires human-in-the-loop confirmation."""
        return False

    @property
    def allowed_agents(self) -> list[str] | None:
        """List of agent types permitted to use this tool, or None for all."""
        return None

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """Execute the tool's core logic with the provided arguments.

        Raises:
            Exception: Any execution failure to be trapped and isolated by ToolExecutor.
        """
        pass

    def to_definition(self) -> ToolDefinition:
        """Export as a ToolDefinition schema object."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters_schema=self.parameters_schema,
            permission=self.permission,
            timeout_seconds=self.timeout_seconds,
            requires_approval=self.requires_approval,
            allowed_agents=self.allowed_agents,
        )

    def to_gemini_declaration(self) -> Any:
        """Convert into a google.genai.types.FunctionDeclaration instance."""
        try:
            from google.genai import types
            return types.FunctionDeclaration(
                name=self.name,
                description=self.description,
                parameters_json_schema=self.parameters_schema or {"type": "object", "properties": {}},
            )
        except ImportError:
            return self.to_definition().to_gemini_declaration_dict()

