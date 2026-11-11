"""Executor Agent for decomposing objectives, tracking progress, and updating task state."""

import logging
import re
from typing import Any

from src.agent.context.builder import ConstructedContext
from src.agents.base import AgentResult, AgentType, BaseAgent
from src.llm.base import ModelTier
from src.memory.manager import MemoryManager
from src.memory.schemas.memory import FactType, MemoryCandidate, MemoryCategory

logger = logging.getLogger(__name__)

_TASK_REGEX = re.compile(
    r"(?:^|\n)\s*(?:[-*•]|\d+\.)\s*(\[(?:TODO|IN_PROGRESS|COMPLETED|BLOCKED|DONE)\]|[A-Z][^:\n]+:)\s*(.+)",
    re.IGNORECASE,
)


class ExecutorAgent(BaseAgent):
    """Specialized task decomposition and execution planner managing persistent task state."""

    @property
    def agent_type(self) -> AgentType:
        return AgentType.EXECUTOR

    @property
    def role_prompt(self) -> str:
        return (
            "You are a structured Execution and Implementation Task Orchestrator. "
            "Your objective is to translate goals into concrete, numbered, sequential steps. "
            "For each task plan:\n"
            "1. Specify Clear Objective & Definition of Done\n"
            "2. Break down into sequential Actionable Steps with status tags: [TODO], [IN_PROGRESS], [COMPLETED], or [BLOCKED]\n"
            "3. Identify Known Dependencies and Blockers\n"
            "4. Immediate Next Step.\n"
            "Keep instructions practical, specific, and unambiguous.\n"
            "Format cleanly for Telegram: use bold text for headers instead of markdown hashes (#), "
            "use bullet points (•) for lists, and avoid horizontal lines (---)."
        )

    def extract_actionable_tasks(self, response_text: str) -> list[str]:
        """Extract structured task lines from generated response."""
        tasks: list[str] = []
        for line in response_text.splitlines():
            line_str = line.strip()
            if any(tag in line_str.upper() for tag in ["[TODO]", "[IN_PROGRESS]", "[BLOCKED]", "[COMPLETED]", "[DONE]"]):
                tasks.append(line_str)
            elif re.match(r"^\d+\.\s+\[?[A-Za-z]", line_str):
                tasks.append(line_str)
        return tasks[:10]

    async def execute(
        self,
        context: ConstructedContext,
        user_text: str,
        user_id: int,
        project_id: str | None = None,
        execution_record: Any = None,
    ) -> AgentResult:
        """Execute task decomposition and record task state in shared memory using execution loop."""
        llm_messages = context.to_llm_messages(model_family="gemini")

        system_instruction = self.role_prompt
        history_turns: list[dict[str, str]] = []

        for msg in llm_messages:
            if msg.get("role") == "system":
                system_instruction = f"{self.role_prompt}\n\n{msg.get('content', '')}"
            else:
                history_turns.append(msg)

        # Multi-step strategic planning benefits from Large tier, progress updates use Medium
        tier = ModelTier.LARGE if "roadmap" in user_text.lower() or "plan" in user_text.lower() else ModelTier.MEDIUM

        loop_result = await self.execution_loop.execute_turn(
            llm_manager=self.llm_manager,
            user_id=user_id,
            messages=history_turns,
            tier=tier,
            system_instruction=system_instruction,
            agent_type=self.agent_type.value,
            allowed_tools=self.allowed_tools,
            execution_record=execution_record,
            timeout=75.0,
        )

        # Extract structured tasks
        extracted_tasks = self.extract_actionable_tasks(loop_result.content)
        task_updates = [{"task": t} for t in extracted_tasks]

        # Update task memories in shared MemoryManager
        if self.memory_manager and extracted_tasks:
            try:
                top_task_summary = "; ".join(extracted_tasks[:3])
                candidate = MemoryCandidate(
                    category=MemoryCategory.TASK,
                    content=f"Plan: {top_task_summary}",
                    fact_type=FactType.PLAN,
                    importance=0.8,
                    confidence=1.0,
                    project_association=project_id,
                )
                await self.memory_manager.remember(
                    user_id=user_id,
                    candidate=candidate,
                    source_reference={"origin": "executor_agent", "turn_text": user_text[:60]},
                )
                logger.info("ExecutorAgent: updated shared task memory for user %d", user_id)
            except Exception as exc:
                logger.warning("ExecutorAgent: non-fatal error recording task memory: %s", exc)

        return AgentResult(
            agent_type=self.agent_type,
            response_text=loop_result.content,
            model_used=loop_result.model_name,
            tier_used=loop_result.tier_used,
            tokens_used=loop_result.tokens_used,
            latency_seconds=loop_result.latency_seconds,
            estimated_cost_usd=loop_result.estimated_cost_usd,
            task_updates=task_updates,
            tool_calls=[tc.model_dump() for tc in loop_result.tool_calls_executed],
            metadata={"extracted_tasks_count": len(extracted_tasks), "tool_count": len(loop_result.tool_calls_executed)},
        )
