"""General Assistant agent handling everyday conversations and personal assistance."""

import logging
from typing import Any

from src.agent.context.builder import ConstructedContext
from src.agents.base import AgentResult, AgentType, BaseAgent
from src.llm.base import ModelTier
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
        execution_record: Any = None,
    ) -> AgentResult:
        """Execute general conversation turn using the shared execution loop."""
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

        loop_result = await self.execution_loop.execute_turn(
            llm_manager=self.llm_manager,
            user_id=user_id,
            messages=history_turns,
            tier=tier,
            system_instruction=system_instruction,
            agent_type=self.agent_type.value,
            allowed_tools=self.allowed_tools,
            execution_record=execution_record,
        )

        return AgentResult(
            agent_type=self.agent_type,
            response_text=loop_result.content,
            model_used=loop_result.model_name,
            tier_used=loop_result.tier_used,
            tokens_used=loop_result.tokens_used,
            latency_seconds=loop_result.latency_seconds,
            estimated_cost_usd=loop_result.estimated_cost_usd,
            tool_calls=[tc.model_dump() for tc in loop_result.tool_calls_executed],
            metadata={"fallback_used": loop_result.fallback_used, "tool_count": len(loop_result.tool_calls_executed)},
        )
