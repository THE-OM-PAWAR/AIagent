"""Model Router selecting optimal model tier and resolving tier-to-model configuration."""

import logging
from typing import Any
from src.llm.base import ModelTier

logger = logging.getLogger(__name__)


class ModelRouter:
    """Intelligently routes requests to the smallest suitable model tier."""

    def __init__(
        self,
        small_model: str = "gemini-2.5-flash",
        medium_model: str = "gemini-2.5-flash",
        large_model: str = "gemini-1.5-pro",
    ) -> None:
        self.tier_models: dict[ModelTier, str] = {
            ModelTier.SMALL: small_model,
            ModelTier.MEDIUM: medium_model,
            ModelTier.LARGE: large_model,
        }

    def resolve_model_name(self, tier: ModelTier) -> str:
        """Return the concrete model string configured for a given tier."""
        return self.tier_models.get(tier, self.tier_models[ModelTier.MEDIUM])

    def select_tier(
        self,
        agent_type: str,
        query: str,
        estimated_context_tokens: int = 0,
        is_analytical: bool = False,
        is_planning: bool = False,
    ) -> ModelTier:
        """Select the most cost-effective model tier based on agent role, complexity, and tokens."""
        clean = query.strip().lower()

        # 1. Analyst Agent or explicit deep analytical requests require Large tier
        if agent_type == "analyst" or is_analytical:
            # Complex technical comparisons, architectural evaluations, deep audits
            return ModelTier.LARGE

        # 2. Executor complex strategic roadmaps
        if agent_type == "executor" and (is_planning or len(clean.split()) > 50):
            return ModelTier.LARGE

        # 3. Simple classification, short casual queries, or extraction tasks use Small tier
        if len(clean.split()) <= 6 and not any(k in clean for k in ["why", "how", "explain", "compare", "plan"]):
            return ModelTier.SMALL

        # 4. Standard default for general conversations and everyday assistance is Medium tier
        return ModelTier.MEDIUM

    def get_fallback_tier(self, current_tier: ModelTier) -> ModelTier | None:
        """Determine next tier in fallback chain if primary model encounters errors."""
        if current_tier == ModelTier.LARGE:
            return ModelTier.MEDIUM
        elif current_tier == ModelTier.MEDIUM:
            return ModelTier.SMALL
        return None

