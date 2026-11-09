"""General Assistant agent handling everyday conversations and personal assistance."""

import logging
from typing import Any

from src.agent.context.builder import ConstructedContext
from src.agents.base import AgentResult, AgentType, BaseAgent
from src.llm.base import ModelTier
from src.llm.manager import LLMManager
from src.memory.manager import MemoryManager

logger = logging.getLogger(__name__)


class GeneralAssistant(BaseAgent):
    """Generalist personal assistant for natural dialogues, personal memory, and everyday queries."""

    @property
    def agent_type(self) -> AgentType:
        return AgentType.GENERAL

    @property
    def role_prompt(self) -> str:
        return (
            "You are a helpful, respectful, and concise personal AI assistant. "
            "Use retrieved personal context naturally without robotic citations. "
            "Be direct, polite, and helpful."
        )

    async def execute(
        self,
        context: ConstructedContext,
        user_text: str,
        user_id: int,
        project_id: str | None = None,
    ) -> AgentResult:
        """Execute general conversation turn."""
        llm_messages = context.to_llm_messages(model_family="gemini")

        system_instruction = context.system_prompt
        history_turns: list[dict[str, str]] = []

        for msg in llm_messages:
            if msg.get("role") == "system":
                system_instruction = msg.get("content", context.system_prompt)
            else:
                history_turns.append(msg)

        clean = user_text.strip()
        tier = ModelTier.SMALL if len(clean.split()) <= 6 else ModelTier.MEDIUM

        response = await self.llm_manager.generate(
            messages=history_turns,
            tier=tier,
            system_instruction=system_instruction,
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
