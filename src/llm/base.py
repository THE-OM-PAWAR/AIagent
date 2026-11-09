"""Base schemas and abstractions for the LLM Layer."""

from abc import ABC, abstractmethod
from enum import Enum
import time
from typing import Any
from pydantic import BaseModel, Field


class ModelTier(str, Enum):
    """Three operational model capability and cost tiers."""
    SMALL = "small"    # Fast classification, extraction, summarization, simple turns
    MEDIUM = "medium"  # Everyday conversation, standard reasoning, personal assistance
    LARGE = "large"    # Complex analysis, strategic evaluations, architectural planning


class LLMMessage(BaseModel):
    """Standardized chat message payload."""
    role: str  # 'system', 'user', 'model' / 'assistant'
    content: str


class LLMResponse(BaseModel):
    """Standardized output from any LLM provider invocation."""
    content: str
    model_name: str
    tier: ModelTier = ModelTier.MEDIUM
    provider: str = "gemini"
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    fallback_used: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseLLMProvider(ABC):
    """Abstract interface that all LLM provider adapters must implement."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the provider (e.g. 'gemini')."""
        pass

    @abstractmethod
    async def generate(
        self,
        messages: list[dict[str, str]],
        model_name: str,
        system_instruction: str | None = None,
        timeout: float = 60.0,
        tier: ModelTier = ModelTier.MEDIUM,
        user_id: int = 101,
    ) -> LLMResponse:
        """Generate a response given conversation turns and system instruction."""
        pass

    @abstractmethod
    async def is_healthy(self) -> bool:
        """Check provider connectivity and health."""
        pass
