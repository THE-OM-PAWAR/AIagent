"""Analyst Agent specializing in deep technical research, tradeoffs, and strategic planning."""

import logging
from typing import Any

from src.agent.context.builder import ConstructedContext
from src.agents.base import AgentResult, AgentType, BaseAgent
from src.llm.base import ModelTier
from src.llm.manager import LLMManager
from src.memory.manager import MemoryManager

logger = logging.getLogger(__name__)


class AnalystAgent(BaseAgent):
    """Specialized analytical agent providing structured technical evaluations, tradeoffs, and risks."""

    @property
    def agent_type(self) -> AgentType:
        return AgentType.ANALYST

    @property
    def role_prompt(self) -> str:
        return (
            "You are an expert Senior Technical and Strategic Systems Analyst. "
            "Your objective is to deliver rigorous, structured, and unbiased evaluations. "
            "Structure your output clearly with:\n"
            "1. Executive Summary\n"
            "2. Comparative Tradeoff Analysis / Key Dimensions\n"
            "3. Technical and Operational Risks\n"
            "4. Actionable Strategic Recommendations.\n"
            "Distinguish verified facts from assumptions, acknowledge unknowns, and be precise."
        )

    async def execute(
        self,
        context: ConstructedContext,
        user_text: str,
        user_id: int,
        project_id: str | None = None,
    ) -> AgentResult:
        """Execute complex analysis using Large model tier."""
        llm_messages = context.to_llm_messages(model_family="gemini")

        system_instruction = self.role_prompt
        history_turns: list[dict[str, str]] = []

        for msg in llm_messages:
            if msg.get("role") == "system":
                system_instruction = f"{self.role_prompt}\n\n{msg.get('content', '')}"
            else:
                history_turns.append(msg)

        # Complex analysis requires the Large model tier (e.g. Gemini 1.5 Pro)
        tier = ModelTier.LARGE

        response = await self.llm_manager.generate(
            messages=history_turns,
            tier=tier,
            system_instruction=system_instruction,
            timeout=90.0,
            user_id=user_id,
        )

        return AgentResult(
            agent_type=self.agent_type,
            response_text=response.content,
            model_used=response.model_name,
            tier_used=response.tier,
            tokens_used=response.total_tokens,
            latency_seconds=response.latency_seconds,
            estimated_cost_usd=response.estimated_cost_usd,
            metadata={"fallback_used": response.fallback_used},
        )
