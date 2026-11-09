"""Google Gemini provider adapter implementing BaseLLMProvider."""

import asyncio
import logging
import time
from typing import Any

from src.agent.context.token_budget import TokenEstimator
from src.intelligence.providers.gemini import (
    GeminiAPIError,
    GeminiClient,
    GeminiError,
    GeminiRateLimitError,
    GeminiTimeoutError,
)
from src.llm.base import BaseLLMProvider, LLMResponse, ModelTier

logger = logging.getLogger(__name__)

# Approximate pricing per 1M tokens for cost tracking
_PRICING = {
    # Gemini 2.5 Flash / Flash
    "flash": {"input": 0.075 / 1_000_000, "output": 0.30 / 1_000_000},
    # Gemini 1.5 Pro / Pro
    "pro": {"input": 1.25 / 1_000_000, "output": 5.00 / 1_000_000},
    # Default fallback
    "default": {"input": 0.10 / 1_000_000, "output": 0.40 / 1_000_000},
}


class GeminiProvider(BaseLLMProvider):
    """LLM Provider adapter for Google Gemini via GenAI SDK."""

    def __init__(self, gemini_client: GeminiClient | Any) -> None:
        self._client = gemini_client
        self._estimator = TokenEstimator()

    @property
    def provider_name(self) -> str:
        return "gemini"

    def estimate_cost(self, model_name: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate execution cost in USD based on model family and token usage."""
        norm = model_name.lower()
        if "pro" in norm:
            rates = _PRICING["pro"]
        elif "flash" in norm:
            rates = _PRICING["flash"]
        else:
            rates = _PRICING["default"]

        cost = (input_tokens * rates["input"]) + (output_tokens * rates["output"])
        return round(cost, 6)

    async def generate(
        self,
        messages: list[dict[str, str]],
        model_name: str,
        system_instruction: str | None = None,
        timeout: float = 60.0,
        tier: ModelTier = ModelTier.MEDIUM,
        user_id: int = 101,
    ) -> LLMResponse:
        """Invoke Gemini with messages, system instruction, and token/cost tracking."""
        start_t = time.perf_counter()

        # Separate history turns from current user message
        history_turns: list[dict[str, str]] = []
        user_message_text = ""

        # Extract system prompt if present in messages
        effective_sys = system_instruction
        valid_turns = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system" and not effective_sys:
                effective_sys = content
            elif role != "system":
                valid_turns.append(m)

        if valid_turns:
            user_message_text = valid_turns[-1].get("content", "")
            history_turns = valid_turns[:-1]

        # Calculate input tokens
        input_tokens = (
            self._estimator.count_tokens(effective_sys)
            + self._estimator.count_message_tokens(valid_turns)
        )

        try:
            # Check if underlying client allows overriding model dynamically
            original_model = getattr(self._client, "_model", None)
            if original_model and original_model != model_name:
                self._client._model = model_name

            # Invoke GeminiClient
            kwargs = {}
            # Only pass system_instruction override if dynamic context facts or specialized agent prompts are present
            has_dynamic_context = effective_sys and any(
                tag in effective_sys
                for tag in [
                    "[USER FACTS",
                    "[PROJECT",
                    "[ACTIVE TASKS",
                    "[CONVERSATION SUMMARY",
                    "Senior Technical and Strategic",
                    "Execution and Implementation",
                ]
            )
            if has_dynamic_context:
                if hasattr(self._client, "generate_response"):
                    import inspect
                    sig = inspect.signature(self._client.generate_response)
                    accepts_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                    if "system_instruction" in sig.parameters or accepts_kw:
                        kwargs["system_instruction"] = effective_sys

            response_text = await self._client.generate_response(
                user_id=user_id,
                message_text=user_message_text,
                history=history_turns,
                **kwargs,
            )
        finally:
            if original_model and getattr(self._client, "_model", None) != original_model:
                self._client._model = original_model

        duration = time.perf_counter() - start_t
        output_tokens = self._estimator.count_tokens(response_text)
        total_tokens = input_tokens + output_tokens
        cost = self.estimate_cost(model_name, input_tokens, output_tokens)

        return LLMResponse(
            content=response_text,
            model_name=model_name,
            tier=tier,
            provider="gemini",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            latency_seconds=round(duration, 4),
            estimated_cost_usd=cost,
        )

    async def is_healthy(self) -> bool:
        """Check if Gemini client is initialized."""
        return hasattr(self._client, "client") and self._client._client is not None
