"""Analyst Agent specializing in deep technical research, tradeoffs, and strategic planning."""

import logging
from typing import Any

from src.agent.context.builder import ConstructedContext
from src.agents.base import AgentResult, AgentType, BaseAgent
from src.llm.base import ModelTier
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
            "Distinguish verified facts from assumptions, acknowledge unknowns, and be precise.\n"
            "Format cleanly for Telegram: use bold text for headers instead of markdown hashes (#), "
            "use bullet points (•) for lists, and avoid horizontal lines (---)."
        )

    async def execute(
        self,
        context: ConstructedContext,
        user_text: str,
        user_id: int,
        project_id: str | None = None,
        execution_record: Any = None,
    ) -> AgentResult:
        """Execute complex analysis using Large model tier through shared execution loop."""
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

        loop_result = await self.execution_loop.execute_turn(
            llm_manager=self.llm_manager,
            user_id=user_id,
            messages=history_turns,
            tier=tier,
            system_instruction=system_instruction,
            agent_type=self.agent_type.value,
            allowed_tools=self.allowed_tools,
            execution_record=execution_record,
            timeout=90.0,
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
