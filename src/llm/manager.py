"""LLM Provider Manager orchestrating providers, model tiers, and fallbacks."""

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Any

from src.llm.base import BaseLLMProvider, LLMResponse, ModelTier
from src.llm.providers.gemini import GeminiProvider
from src.llm.router import ModelRouter
from src.tools.schemas import ToolCall, ToolResult

logger = logging.getLogger(__name__)


class LLMManager:
    """Manages LLM providers, model tiers, fallback execution, and token/cost telemetry."""

    def __init__(
        self,
        primary_provider: BaseLLMProvider,
        router: ModelRouter | None = None,
    ) -> None:
        self.provider = primary_provider
        self.router = router or ModelRouter()

        # Telemetry metrics
        self.total_requests: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cost_usd: float = 0.0
        self.fallbacks_triggered: int = 0
        self.tier_counts: dict[ModelTier, int] = {
            ModelTier.SMALL: 0,
            ModelTier.MEDIUM: 0,
            ModelTier.LARGE: 0,
        }

    async def generate(
        self,
        messages: list[dict[str, str]],
        tier: ModelTier = ModelTier.MEDIUM,
        system_instruction: str | None = None,
        timeout: float = 60.0,
        user_id: int = 101,
        tools: list[Any] | None = None,
        tool_callback: Callable[[list[ToolCall]], Awaitable[list[ToolResult]]] | None = None,
        max_iterations: int = 5,
    ) -> LLMResponse:
        """Execute model generation with automatic fallback if primary model fails."""
        current_tier = tier
        attempted_tiers: list[ModelTier] = []
        last_error: Exception | None = None

        while current_tier is not None:
            attempted_tiers.append(current_tier)
            model_name = self.router.resolve_model_name(current_tier)
            logger.info(
                "LLMManager: invoking provider='%s', tier='%s', model='%s' (tools=%s)",
                self.provider.provider_name,
                current_tier.value,
                model_name,
                len(tools) if tools else 0,
            )

            try:
                response = await self.provider.generate(
                    messages=messages,
                    model_name=model_name,
                    system_instruction=system_instruction,
                    timeout=timeout,
                    tier=current_tier,
                    user_id=user_id,
                    tools=tools,
                    tool_callback=tool_callback,
                    max_iterations=max_iterations,
                )

                # Record metrics
                self.total_requests += 1
                self.total_input_tokens += response.input_tokens
                self.total_output_tokens += response.output_tokens
                self.total_cost_usd += response.estimated_cost_usd
                self.tier_counts[current_tier] = self.tier_counts.get(current_tier, 0) + 1

                if len(attempted_tiers) > 1:
                    response.fallback_used = True
                    self.fallbacks_triggered += 1
                    logger.warning(
                        "LLMManager: successfully recovered via fallback tier '%s' (initial: '%s')",
                        current_tier.value,
                        attempted_tiers[0].value,
                    )

                return response

            except Exception as exc:
                last_error = exc
                next_tier = self.router.get_fallback_tier(current_tier)
                logger.warning(
                    "LLMManager: tier '%s' (%s) failed with: %s. Fallback candidate: %s",
                    current_tier.value,
                    model_name,
                    exc,
                    next_tier.value if next_tier else "None",
                )
                current_tier = next_tier

        # All tiers exhausted
        logger.error("LLMManager: all model tiers failed: %s", attempted_tiers)
        raise last_error or RuntimeError("All model tiers failed.")

    def get_metrics(self) -> dict[str, Any]:
        """Return cumulative telemetry and cost tracking data."""
        return {
            "total_requests": self.total_requests,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "fallbacks_triggered": self.fallbacks_triggered,
            "tier_distribution": {k.value: v for k, v in self.tier_counts.items()},
        }
