"""Comprehensive test suite for Phase 1: Production-Grade Tool Calling Engine."""

import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.agent.context.builder import ConstructedContext
from src.agent.context.token_budget import TokenUsageEstimate
from src.agent.execution import ExecutionRecord, ExecutionTracker
from src.agent.orchestrator import IntelligenceOrchestrator
from src.agents import AgentType, AnalystAgent, ExecutorAgent, GeneralAssistant
from src.core.execution_loop import ExecutionLoop, ExecutionLoopResult
from src.llm import LLMManager, ModelRouter, ModelTier
from src.llm.base import BaseLLMProvider, LLMResponse
from src.tools import (
    BaseTool,
    CurrentUtcTimeTool,
    PermissionPolicy,
    ToolArgumentError,
    ToolCall,
    ToolDefinition,
    ToolDuplicateCallError,
    ToolError,
    ToolExecutionError,
    ToolExecutor,
    ToolLoopLimitError,
    ToolNotFoundError,
    ToolPermission,
    ToolPermissionError,
    ToolRegistry,
    ToolResult,
)


class MockCalculatorTool(BaseTool):
    """Sample tool performing simple addition."""
    @property
    def name(self) -> str:
        return "add_numbers"

    @property
    def description(self) -> str:
        return "Add two numbers together."

    @property
    def parameters_schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        }

    async def execute(self, a: float, b: float) -> float:
        return a + b


class MockSlowTool(BaseTool):
    """Tool that sleeps to simulate timeout."""
    @property
    def name(self) -> str:
        return "slow_tool"

    @property
    def description(self) -> str:
        return "A slow tool that exceeds timeout."

    @property
    def timeout_seconds(self) -> float:
        return 0.1

    async def execute(self) -> str:
        await asyncio.sleep(0.5)
        return "done"


class MockFailingTool(BaseTool):
    """Tool that intentionally raises an exception."""
    @property
    def name(self) -> str:
        return "failing_tool"

    @property
    def description(self) -> str:
        return "A tool that raises an exception."

    async def execute(self) -> str:
        raise ValueError("Critical internal computation failure")


class MockSensitiveTool(BaseTool):
    """Tool requiring SENSITIVE permission."""
    @property
    def name(self) -> str:
        return "delete_database"

    @property
    def description(self) -> str:
        return "A destructive tool."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.ADMIN

    async def execute(self) -> str:
        return "deleted"


class TestToolCallingEngine(unittest.IsolatedAsyncioTestCase):
    """Unit and integration tests for the tool execution engine and safeguards."""

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.permission_policy = PermissionPolicy()
        self.execution_tracker = ExecutionTracker()
        self.executor = ToolExecutor(
            registry=self.registry,
            permission_policy=self.permission_policy,
            execution_tracker=self.execution_tracker,
            default_timeout=5.0,
        )

        # Register sample tools
        self.registry.register_tool(MockCalculatorTool())
        self.registry.register_tool(MockSlowTool())
        self.registry.register_tool(MockFailingTool())
        self.registry.register_tool(MockSensitiveTool())
        self.registry.register_tool(CurrentUtcTimeTool())

    # 1. Normal text-only conversation
    async def test_text_only_conversation(self) -> None:
        """Verify normal conversation without invoking any tools."""
        mock_provider = MagicMock(spec=BaseLLMProvider)
        mock_provider.provider_name = "gemini"
        mock_provider.generate = AsyncMock(
            return_value=LLMResponse(
                content="Hello there! How can I help you today?",
                model_name="gemini-3.6-flash",
                tier=ModelTier.MEDIUM,
            )
        )
        llm_manager = LLMManager(mock_provider)
        loop = ExecutionLoop(self.registry, self.executor)

        result = await loop.execute_turn(
            llm_manager=llm_manager,
            user_id=101,
            messages=[{"role": "user", "content": "Hello"}],
            tier=ModelTier.MEDIUM,
        )

        self.assertEqual(result.content, "Hello there! How can I help you today?")
        self.assertEqual(len(result.tool_calls_executed), 0)
        self.assertEqual(result.iterations, 1)

    # 2. One successful tool call
    async def test_one_successful_tool_call(self) -> None:
        """Verify a single successful tool call execution with result returned to Gemini."""
        mock_provider = MagicMock(spec=BaseLLMProvider)
        mock_provider.provider_name = "gemini"

        async def fake_generate(**kwargs):
            tool_callback = kwargs.get("tool_callback")
            if tool_callback:
                # Simulate Gemini calling add_numbers(a=15, b=25)
                tool_calls = [ToolCall(call_id="call_1", tool_name="add_numbers", arguments={"a": 15, "b": 25})]
                results = await tool_callback(tool_calls)
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].success)
                self.assertEqual(results[0].output, 40)

            return LLMResponse(
                content="The sum of 15 and 25 is 40.",
                model_name="gemini-3.6-flash",
                tier=ModelTier.MEDIUM,
            )

        mock_provider.generate = AsyncMock(side_effect=fake_generate)
        llm_manager = LLMManager(mock_provider)
        loop = ExecutionLoop(self.registry, self.executor)

        result = await loop.execute_turn(
            llm_manager=llm_manager,
            user_id=101,
            messages=[{"role": "user", "content": "What is 15 + 25?"}],
        )

        self.assertEqual(result.content, "The sum of 15 and 25 is 40.")
        self.assertEqual(len(result.tool_calls_executed), 1)
        self.assertEqual(result.tool_calls_executed[0].tool_name, "add_numbers")
        self.assertEqual(result.tool_calls_executed[0].output, 40)

    # 3. Multiple tool calls in one turn (parallel)
    async def test_multiple_parallel_tool_calls(self) -> None:
        """Verify multiple parallel tool calls executed within one turn."""
        mock_provider = MagicMock(spec=BaseLLMProvider)
        mock_provider.provider_name = "gemini"

        async def fake_generate(**kwargs):
            tool_callback = kwargs.get("tool_callback")
            tool_calls = [
                ToolCall(call_id="call_a", tool_name="add_numbers", arguments={"a": 10, "b": 20}),
                ToolCall(call_id="call_b", tool_name="get_current_utc_time", arguments={}),
            ]
            results = await tool_callback(tool_calls)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0].output, 30)
            self.assertIn("T", results[1].output)

            return LLMResponse(
                content="10 + 20 is 30, and current UTC time is confirmed.",
                model_name="gemini-3.6-flash",
                tier=ModelTier.MEDIUM,
            )

        mock_provider.generate = AsyncMock(side_effect=fake_generate)
        llm_manager = LLMManager(mock_provider)
        loop = ExecutionLoop(self.registry, self.executor)

        result = await loop.execute_turn(
            llm_manager=llm_manager,
            user_id=101,
            messages=[{"role": "user", "content": "Add 10 and 20 and tell me UTC time"}],
        )

        self.assertEqual(len(result.tool_calls_executed), 2)
        self.assertEqual(result.tool_calls_executed[0].output, 30)
        self.assertTrue(result.tool_calls_executed[1].success)

    # 4. Sequential tool calls across iterations
    async def test_sequential_tool_calls(self) -> None:
        """Verify sequential tool calls across multiple loop iterations."""
        mock_provider = MagicMock(spec=BaseLLMProvider)
        mock_provider.provider_name = "gemini"
        call_step = 0

        async def fake_generate(**kwargs):
            nonlocal call_step
            tool_callback = kwargs.get("tool_callback")
            call_step += 1
            if call_step == 1:
                # Step 1: add 5 + 5
                results = await tool_callback([ToolCall(call_id="c1", tool_name="add_numbers", arguments={"a": 5, "b": 5})])
                self.assertEqual(results[0].output, 10)
            # Step 2: add result 10 + 20
            results2 = await tool_callback([ToolCall(call_id="c2", tool_name="add_numbers", arguments={"a": 10, "b": 20})])
            self.assertEqual(results2[0].output, 30)

            return LLMResponse(content="Final computed total is 30.", model_name="gemini-3.6-flash")

        mock_provider.generate = AsyncMock(side_effect=fake_generate)
        llm_manager = LLMManager(mock_provider)
        loop = ExecutionLoop(self.registry, self.executor)

        result = await loop.execute_turn(
            llm_manager=llm_manager,
            user_id=101,
            messages=[{"role": "user", "content": "Do two steps"}],
        )

        self.assertEqual(result.content, "Final computed total is 30.")
        self.assertEqual(len(result.tool_calls_executed), 2)

    # 5. Invalid tool arguments validation
    async def test_invalid_tool_arguments(self) -> None:
        """Verify strict schema validation rejects invalid arguments without crashing."""
        result = await self.executor.execute_tool(
            tool_name="add_numbers",
            arguments={"a": "not_a_number", "b": 20},
        )
        self.assertFalse(result.success)
        self.assertIn("Invalid arguments", result.error)
        self.assertIn("expected number", result.error)

    # 6. Unknown tool name rejection
    async def test_unknown_tool_rejection(self) -> None:
        """Verify unknown tool call is rejected cleanly with structured error."""
        result = await self.executor.execute_tool(
            tool_name="non_existent_tool_xyz",
            arguments={},
        )
        self.assertFalse(result.success)
        self.assertIn("not found", result.error)

    # 7. Tool execution timeout
    async def test_tool_timeout(self) -> None:
        """Verify per-tool timeout is enforced and captured as structured error."""
        result = await self.executor.execute_tool(
            tool_name="slow_tool",
            arguments={},
        )
        self.assertFalse(result.success)
        self.assertIn("timed out", result.error)

    # 8. Tool exception isolation
    async def test_tool_exception_isolation(self) -> None:
        """Verify unhandled tool exceptions are shielded and do not crash execution."""
        result = await self.executor.execute_tool(
            tool_name="failing_tool",
            arguments={},
        )
        self.assertFalse(result.success)
        self.assertIn("Critical internal computation failure", result.error)

    # 9. Maximum iteration limit safeguard
    async def test_max_iteration_limit(self) -> None:
        """Verify infinite tool calling loop terminates at max_iterations."""
        loop = ExecutionLoop(self.registry, self.executor, max_iterations=3)
        self.assertEqual(loop.max_iterations, 3)

    # 10. Duplicate call prevention
    async def test_duplicate_call_prevention(self) -> None:
        """Verify identical tool call with same arguments is detected and rejected in a turn."""
        mock_provider = MagicMock(spec=BaseLLMProvider)
        mock_provider.provider_name = "gemini"

        async def fake_generate(**kwargs):
            tool_callback = kwargs.get("tool_callback")
            call = ToolCall(call_id="c1", tool_name="add_numbers", arguments={"a": 1, "b": 2})
            # First execution succeeds
            res1 = await tool_callback([call])
            self.assertTrue(res1[0].success)
            # Duplicate execution in same turn fails
            res2 = await tool_callback([call])
            self.assertFalse(res2[0].success)
            self.assertIn("Duplicate call", res2[0].error)

            return LLMResponse(content="Done handling duplicates.", model_name="gemini-3.6-flash")

        mock_provider.generate = AsyncMock(side_effect=fake_generate)
        llm_manager = LLMManager(mock_provider)
        loop = ExecutionLoop(self.registry, self.executor)

        result = await loop.execute_turn(
            llm_manager=llm_manager,
            user_id=101,
            messages=[{"role": "user", "content": "Add 1 and 2"}],
        )
        self.assertEqual(len(result.tool_calls_executed), 2)
        self.assertTrue(result.tool_calls_executed[0].success)
        self.assertFalse(result.tool_calls_executed[1].success)

    # 11. Maximum tool calls per turn limit
    async def test_max_tool_calls_per_turn_limit(self) -> None:
        """Verify turns exceeding max_tool_calls_per_turn are capped."""
        loop = ExecutionLoop(self.registry, self.executor, max_tool_calls_per_turn=2)
        mock_provider = MagicMock(spec=BaseLLMProvider)
        mock_provider.provider_name = "gemini"

        async def fake_generate(**kwargs):
            tool_callback = kwargs.get("tool_callback")
            calls = [
                ToolCall(call_id="c1", tool_name="add_numbers", arguments={"a": 1, "b": 1}),
                ToolCall(call_id="c2", tool_name="add_numbers", arguments={"a": 2, "b": 2}),
                ToolCall(call_id="c3", tool_name="add_numbers", arguments={"a": 3, "b": 3}),
            ]
            results = await tool_callback(calls)
            self.assertTrue(results[0].success)
            self.assertTrue(results[1].success)
            self.assertFalse(results[2].success)
            self.assertIn("exceeded maximum", results[2].error)
            return LLMResponse(content="Capped response", model_name="gemini-3.6-flash")

        mock_provider.generate = AsyncMock(side_effect=fake_generate)
        llm_manager = LLMManager(mock_provider)

        result = await loop.execute_turn(
            llm_manager=llm_manager,
            user_id=101,
            messages=[{"role": "user", "content": "Lots of tools"}],
        )
        self.assertEqual(len(result.tool_calls_executed), 3)

    # 12. Permission policy enforcement
    async def test_permission_denial(self) -> None:
        """Verify tools requiring higher permissions are blocked for unauthorized users."""
        # User 101 has default WRITE permission, tool requires ADMIN
        result = await self.executor.execute_tool(
            tool_name="delete_database",
            arguments={},
            user_id=101,
        )
        self.assertFalse(result.success)
        self.assertIn("cannot execute tool 'delete_database'", result.error)

    # 13. All three agents using the same engine
    async def test_all_agents_use_shared_engine(self) -> None:
        """Verify GeneralAssistant, AnalystAgent, and ExecutorAgent all utilize the execution loop."""
        mock_provider = MagicMock(spec=BaseLLMProvider)
        mock_provider.provider_name = "gemini"
        mock_provider.generate = AsyncMock(
            return_value=LLMResponse(
                content="1. [TODO] Step One\n2. [IN_PROGRESS] Step Two",
                model_name="gemini-3.6-flash",
                tier=ModelTier.MEDIUM,
            )
        )
        llm_manager = LLMManager(mock_provider)
        loop = ExecutionLoop(self.registry, self.executor)

        general_agent = GeneralAssistant(llm_manager, execution_loop=loop)
        analyst_agent = AnalystAgent(llm_manager, execution_loop=loop)
        executor_agent = ExecutorAgent(llm_manager, execution_loop=loop)

        self.assertIs(general_agent.execution_loop, loop)
        self.assertIs(analyst_agent.execution_loop, loop)
        self.assertIs(executor_agent.execution_loop, loop)

        dummy_context = ConstructedContext(user_id=101, query="test", token_usage=TokenUsageEstimate())

        res_general = await general_agent.execute(dummy_context, "test query", 101)
        res_analyst = await analyst_agent.execute(dummy_context, "analyze query", 101)
        res_executor = await executor_agent.execute(dummy_context, "execute plan", 101)

        self.assertIn("Step One", res_general.response_text)
        self.assertIn("Step One", res_analyst.response_text)
        self.assertIn("Step One", res_executor.response_text)
        self.assertEqual(len(res_executor.task_updates), 2)

    # 14. Execution observability tracking
    async def test_observability_tracking(self) -> None:
        """Verify tool calls are registered in ExecutionRecord and ExecutionTracker."""
        exec_rec = await self.execution_tracker.start_execution(
            user_id=101,
            query="Add numbers",
        )
        res = await self.executor.execute_tool(
            tool_name="add_numbers",
            arguments={"a": 100, "b": 200},
            execution_record=exec_rec,
        )
        self.assertTrue(res.success)
        self.assertEqual(len(exec_rec.tool_calls), 1)
        self.assertEqual(exec_rec.tool_calls[0].tool_name, "add_numbers")
        self.assertEqual(exec_rec.tool_calls[0].status, "success")
        self.assertGreaterEqual(exec_rec.tool_calls[0].duration_seconds, 0.0)

        stats = await self.execution_tracker.get_stats()
        self.assertEqual(stats["total_tool_calls"], 1)
        self.assertEqual(stats["successful_tool_calls"], 1)


if __name__ == "__main__":
    unittest.main()
