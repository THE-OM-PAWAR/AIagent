"""Model-specific token budgeting and provider-compatible token estimation."""

import logging
import re
from typing import Any
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Try importing tiktoken for provider-compatible BPE token counting
try:
    import tiktoken
    _TIKTOKEN_AVAILABLE = True
except ImportError:
    tiktoken = None
    _TIKTOKEN_AVAILABLE = False


class ModelSpec(BaseModel):
    """Specification and token budget limits for a given model."""
    model_name: str
    max_context_window: int = 32768
    default_token_budget: int = 16384
    reserved_response_tokens: int = 4096
    max_history_share: float = 0.45
    max_memory_share: float = 0.40
    max_summary_share: float = 0.15


# Well-known model specifications
MODEL_SPECS: dict[str, ModelSpec] = {
    "gemini-2.5-flash": ModelSpec(
        model_name="gemini-2.5-flash",
        max_context_window=1048576,
        default_token_budget=32768,
        reserved_response_tokens=4096,
    ),
    "gemini-2.0-flash": ModelSpec(
        model_name="gemini-2.0-flash",
        max_context_window=1048576,
        default_token_budget=32768,
        reserved_response_tokens=4096,
    ),
    "gemini-1.5-pro": ModelSpec(
        model_name="gemini-1.5-pro",
        max_context_window=2097152,
        default_token_budget=65536,
        reserved_response_tokens=8192,
    ),
    "gemini-1.5-flash": ModelSpec(
        model_name="gemini-1.5-flash",
        max_context_window=1048576,
        default_token_budget=32768,
        reserved_response_tokens=4096,
    ),
    "gpt-4o": ModelSpec(
        model_name="gpt-4o",
        max_context_window=128000,
        default_token_budget=32768,
        reserved_response_tokens=4096,
    ),
    "claude-3-5-sonnet": ModelSpec(
        model_name="claude-3-5-sonnet",
        max_context_window=200000,
        default_token_budget=32768,
        reserved_response_tokens=4096,
    ),
    "default": ModelSpec(
        model_name="default",
        max_context_window=16384,
        default_token_budget=8192,
        reserved_response_tokens=2048,
    ),
}


class TokenUsageEstimate(BaseModel):
    """Detailed breakdown of estimated token consumption across context sources."""
    system_tokens: int = 0
    history_tokens: int = 0
    memory_tokens: int = 0
    summary_tokens: int = 0
    reserved_response_tokens: int = 0
    total_used_tokens: int = 0
    available_budget: int = 0
    max_budget: int = 0
    pruned_items_count: int = 0


class TokenEstimator:
    """Accurate token estimator using tiktoken BPE with calibrated fallback."""

    def __init__(self) -> None:
        self._encoder = None
        if _TIKTOKEN_AVAILABLE:
            try:
                self._encoder = tiktoken.get_encoding("cl100k_base")
            except Exception as e:
                logger.warning("Could not initialize cl100k_base tokenizer: %s. Using fallback.", e)
                self._encoder = None

    def count_tokens(self, text: str | None) -> int:
        """Estimate token count for a text string."""
        if not text:
            return 0

        if self._encoder is not None:
            try:
                return len(self._encoder.encode(text, disallowed_special=()))
            except Exception:
                pass

        # Robust calibrated fallback:
        # Regex tokenizes words, numbers, and punctuation sequences
        # English/code typically averages ~1.25 tokens per word/symbol
        tokens = re.findall(r"\w+|[^\w\s]", text, re.UNICODE)
        return max(1, int(len(tokens) * 1.25))

    def count_message_tokens(self, messages: list[dict[str, Any]] | list[Any]) -> int:
        """Count tokens across a list of message dicts or objects, including role overhead."""
        total = 0
        for msg in messages:
            if isinstance(msg, dict):
                role = str(msg.get("role", ""))
                content = str(msg.get("content", ""))
            else:
                role = getattr(msg, "role", "")
                content = getattr(msg, "content", "")
            # ~4 tokens per message turn for framing/delimiters
            total += 4 + self.count_tokens(role) + self.count_tokens(content)
        return total


class TokenBudgetManager:
    """Manages context token budgets, allocation across sources, and pruning."""

    def __init__(
        self,
        default_model: str = "gemini-2.5-flash",
        custom_specs: dict[str, ModelSpec] | None = None,
    ) -> None:
        self.default_model = default_model
        self.specs = dict(MODEL_SPECS)
        if custom_specs:
            self.specs.update(custom_specs)
        self.estimator = TokenEstimator()

    def get_model_spec(self, model_name: str | None) -> ModelSpec:
        """Resolve specification for a model name or return default."""
        if not model_name:
            model_name = self.default_model
        norm = model_name.strip().lower()
        if norm in self.specs:
            return self.specs[norm]
        # Match prefix (e.g. gemini-2.5-flash-latest -> gemini-2.5-flash)
        for key, spec in self.specs.items():
            if norm.startswith(key):
                return spec
        return self.specs["default"]

    def calculate_effective_budget(
        self,
        model_name: str | None = None,
        max_budget: int | None = None,
        reserved_response: int | None = None,
    ) -> tuple[int, int]:
        """Calculate effective available context budget and reserved response tokens.

        Returns:
            (usable_context_budget, reserved_response_tokens)
        """
        spec = self.get_model_spec(model_name)
        total_limit = max_budget if max_budget is not None else spec.default_token_budget
        # Ensure budget never exceeds the physical context window
        total_limit = min(total_limit, spec.max_context_window)

        if reserved_response is not None:
            reserved = reserved_response
        else:
            # If total_limit is smaller than default reserved response, scale reserved proportionally (e.g. 20%)
            if total_limit <= spec.reserved_response_tokens * 2:
                reserved = max(10, int(total_limit * 0.20))
            else:
                reserved = spec.reserved_response_tokens

        usable = max(10, total_limit - reserved)
        return usable, reserved

    def estimate_usage(
        self,
        system_prompt: str | None,
        messages: list[Any],
        memories: list[Any],
        summary: str | None,
        reserved_response: int,
        max_budget: int,
    ) -> TokenUsageEstimate:
        """Calculate detailed token usage estimate."""
        sys_tokens = self.estimator.count_tokens(system_prompt)
        hist_tokens = self.estimator.count_message_tokens(messages)
        mem_tokens = sum(
            self.estimator.count_tokens(getattr(m, "content", str(m)))
            for m in memories
        )
        summ_tokens = self.estimator.count_tokens(summary)
        total = sys_tokens + hist_tokens + mem_tokens + summ_tokens + reserved_response
        usable = max(0, max_budget - reserved_response)

        return TokenUsageEstimate(
            system_tokens=sys_tokens,
            history_tokens=hist_tokens,
            memory_tokens=mem_tokens,
            summary_tokens=summ_tokens,
            reserved_response_tokens=reserved_response,
            total_used_tokens=total,
            available_budget=usable,
            max_budget=max_budget,
            pruned_items_count=0,
        )
