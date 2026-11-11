"""Agent execution loop for managing single-turn and multi-turn reasoning steps with tools."""

from collections.abc import Callable
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any
from pydantic import BaseModel, Field

from src.llm.base import LLMResponse, ModelTier
from src.llm.manager import LLMManager
from src.tools.errors import ToolDuplicateCallError, ToolLoopLimitError
from src.tools.executor import ToolExecutor
from src.tools.registry import ToolRegistry
from src.tools.schemas import ToolCall, ToolResult

if TYPE_CHECKING:
    from src.agent.execution import ExecutionRecord, ExecutionTracker

logger = logging.getLogger(__name__)


class ExecutionLoopResult(BaseModel):
    """Result of an execution loop turn including response content and tool call telemetry."""
    content: str
    model_name: str
    tier_used: ModelTier
    tokens_used: int = 0
    latency_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    fallback_used: bool = False
    tool_calls_executed: list[ToolResult] = Field(default_factory=list)
    iterations: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionLoop:
    """Manages execution cycles between user input, intelligence providers, and tools."""

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        tool_executor: ToolExecutor | None = None,
        max_iterations: int = 5,
        max_tool_calls_per_turn: int = 10,
        execution_tracker: Any = None,
    ) -> None:
        self.tool_registry = tool_registry or ToolRegistry()
        self.tool_executor = tool_executor or ToolExecutor(self.tool_registry)
        self.max_iterations = max_iterations
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.execution_tracker = execution_tracker

    async def execute_turn(
        self,
        llm_manager: LLMManager,
        user_id: int,
        messages: list[dict[str, str]],
        tier: ModelTier = ModelTier.MEDIUM,
        system_instruction: str | None = None,
        agent_type: str = "general",
        allowed_tools: list[str] | None = None,
        execution_record: Any = None,
        timeout: float = 60.0,
    ) -> ExecutionLoopResult:
        """Execute a reasoning turn with tool calling safeguards and loop tracking."""
        # Retrieve Gemini-compatible tool object if tools are registered
        tools_list = None
        gemini_tool = self.tool_registry.get_gemini_tool(agent_type=agent_type)
        if gemini_tool:
            tools_list = [gemini_tool]

        executed_tool_results: list[ToolResult] = []
        seen_call_hashes: set[str] = set()
        iterations_count = 0

        async def _tool_callback(tool_calls: list[ToolCall]) -> list[ToolResult]:
            nonlocal iterations_count
            iterations_count += 1
            batch_results: list[ToolResult] = []

            for call in tool_calls:
                # 1. Enforce maximum tool calls per turn limit
                if len(executed_tool_results) >= self.max_tool_calls_per_turn:
                    err_msg = f"Turn exceeded maximum allowed tool calls limit ({self.max_tool_calls_per_turn})."
                    logger.warning("ExecutionLoop: %s", err_msg)
                    res = ToolResult(
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        success=False,
                        error=err_msg,
                    )
                    batch_results.append(res)
                    executed_tool_results.append(res)
                    continue

                # 2. Check for duplicate tool calls to prevent infinite loops
                try:
                    args_serialized = json.dumps(call.arguments, sort_keys=True)
                except Exception:
                    args_serialized = str(call.arguments)
                call_hash = f"{call.tool_name}:{hashlib.sha256(args_serialized.encode()).hexdigest()}"

                if call_hash in seen_call_hashes:
                    err_msg = f"Duplicate call to '{call.tool_name}' with identical arguments detected; rejected to prevent infinite loop."
                    logger.warning("ExecutionLoop: %s", err_msg)
                    res = ToolResult(
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        success=False,
                        error=err_msg,
                    )
                    batch_results.append(res)
                    executed_tool_results.append(res)
                    continue

                seen_call_hashes.add(call_hash)

                # 3. Check allowed tools filter if specified
                if allowed_tools is not None and call.tool_name not in allowed_tools:
                    err_msg = f"Tool '{call.tool_name}' is not permitted for this task."
                    logger.warning("ExecutionLoop: %s", err_msg)
                    res = ToolResult(
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        success=False,
                        error=err_msg,
                    )
                    batch_results.append(res)
                    executed_tool_results.append(res)
                    continue

                # 4. Execute tool via safe ToolExecutor
                tool_res = await self.tool_executor.execute_tool(
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    call_id=call.call_id,
                    user_id=user_id,
                    agent_type=agent_type,
                    execution_record=execution_record,
                )
                batch_results.append(tool_res)
                executed_tool_results.append(tool_res)

            return batch_results

        # Call LLM through LLMManager with tool callback
        llm_response: LLMResponse = await llm_manager.generate(
            messages=messages,
            tier=tier,
            system_instruction=system_instruction,
            timeout=timeout,
            user_id=user_id,
            tools=tools_list,
            tool_callback=_tool_callback if tools_list else None,
            max_iterations=self.max_iterations,
        )

        return ExecutionLoopResult(
            content=llm_response.content,
            model_name=llm_response.model_name,
            tier_used=llm_response.tier,
            tokens_used=llm_response.total_tokens,
            latency_seconds=llm_response.latency_seconds,
            estimated_cost_usd=llm_response.estimated_cost_usd,
            fallback_used=llm_response.fallback_used,
            tool_calls_executed=executed_tool_results,
            iterations=max(1, iterations_count),
            metadata={"tool_count": len(executed_tool_results)},
        )
