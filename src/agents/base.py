"""Base abstractions and schemas for specialized logical agents."""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field

from src.agent.context.builder import ConstructedContext
from src.llm.base import ModelTier
from src.llm.manager import LLMManager
from src.memory.manager import MemoryManager


class AgentType(str, Enum):
    """The three specialized agent types."""
    GENERAL = "general"    # General conversation, everyday assistant, natural memory usage
    ANALYST = "analyst"    # Complex analysis, architectural tradeoffs, deep evaluation
    EXECUTOR = "executor"  # Actionable planning, task breakdown, blocker identification


class AgentResult(BaseModel):
    """Complete output produced by an executed agent."""
    agent_type: AgentType
    response_text: str
    model_used: str
    tier_used: ModelTier
    tokens_used: int = 0
    latency_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    task_updates: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseAgent(ABC):
    """Abstract base class for specialized agents sharing infrastructure."""

    def __init__(
        self,
        llm_manager: LLMManager,
        memory_manager: MemoryManager | None = None,
    ) -> None:
        self.llm_manager = llm_manager
        self.memory_manager = memory_manager

    @property
    @abstractmethod
    def agent_type(self) -> AgentType:
        """The specific role identifier of this agent."""
        pass

    @property
    @abstractmethod
    def role_prompt(self) -> str:
        """Specialized system prompt instructions for this agent role."""
        pass

    @abstractmethod
    async def execute(
        self,
        context: ConstructedContext,
        user_text: str,
        user_id: int,
        project_id: str | None = None,
    ) -> AgentResult:
        """Execute the agent's designated responsibility within shared context."""
        pass

