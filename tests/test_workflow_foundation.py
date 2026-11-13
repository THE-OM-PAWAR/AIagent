"""Comprehensive test suite for the Core Workflow Foundation & Execution Engine.

Covers:
- Workflow creation and validation (schema, naming, unique steps, DAG dependency rules).
- Sequential step execution and output passing via context interpolation.
- Conditional execution (skipping, downstream dependency handling).
- Tool execution, retries, and failure handling.
- Isolated execution context across concurrent workflow runs.
- Workflow definition, step, and run persistence in async DB.
- WorkflowPlanner agent integration (natural language to validated workflow).
- Telegram /workflows subcommands.
"""

import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from telegram import Chat, Message, Update, User

from src.core.workflow_engine import WorkflowEngine, evaluate_condition
from src.core.workflow_manager import WorkflowManager, WorkflowValidationError
from src.core.workflow_planner import WorkflowPlanner
from src.database.models import Base
from src.database.models.workflow import WorkflowModel, WorkflowRunModel, WorkflowStepModel
from src.database.repositories.workflow_repo import WorkflowRepository
from src.interfaces.telegram.handlers import workflows_command
from src.tools.base import BaseTool
from src.tools.executor import ToolExecutor
from src.tools.permissions import PermissionPolicy, ToolPermission
from src.tools.registry import ToolRegistry
from src.tools.schemas import ToolResult


class AddNumbersMockTool(BaseTool):
    @property
    def name(self) -> str:
        return "add_tool"

    @property
    def description(self) -> str:
        return "Add two numbers"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, a: float, b: float, **kwargs) -> dict:
        return {"sum": a + b, "count": 2}


class MultiplyNumbersMockTool(BaseTool):
    @property
    def name(self) -> str:
        return "multiply_tool"

    @property
    def description(self) -> str:
        return "Multiply two numbers"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "factor": {"type": "number"},
            },
            "required": ["x", "factor"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, x: float, factor: float, **kwargs) -> dict:
        return {"product": x * factor}


class FailingMockTool(BaseTool):
    def __init__(self, fail_times: int = 10) -> None:
        self.fail_times = fail_times
        self.call_count = 0

    @property
    def name(self) -> str:
        return "failing_tool"

    @property
    def description(self) -> str:
        return "Fails on demand"

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object"}

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, **kwargs) -> dict:
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise RuntimeError("Temporary simulated failure")
        return {"recovered": True}


class TestWorkflowValidation(unittest.TestCase):
    """Test structural, schema, permission, and DAG cycle validation."""

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register_tool(AddNumbersMockTool())
        self.registry.register_tool(MultiplyNumbersMockTool())
        self.policy = PermissionPolicy()
        self.session_factory = MagicMock()
        self.manager = WorkflowManager(
            session_factory=self.session_factory,
            tool_registry=self.registry,
            permission_policy=self.policy,
        )

    def test_valid_workflow_passes_validation(self) -> None:
        steps = [
            {"name": "step_1", "tool_name": "add_tool", "arguments": {"a": 1, "b": 2}},
            {"name": "step_2", "tool_name": "multiply_tool", "arguments": {"x": "{step_1.sum}", "factor": 3}, "dependencies": ["step_1"]},
        ]
        # Should not raise
        self.manager.validate_workflow_definition("Test Flow", steps, user_id=101)

    def test_invalid_name_fails(self) -> None:
        steps = [{"name": "s1", "tool_name": "add_tool", "arguments": {"a": 1, "b": 2}}]
        with self.assertRaises(WorkflowValidationError) as ctx:
            self.manager.validate_workflow_definition("", steps)
        self.assertIn("Workflow name must be a non-empty string", str(ctx.exception))

    def test_empty_steps_fails(self) -> None:
        with self.assertRaises(WorkflowValidationError) as ctx:
            self.manager.validate_workflow_definition("Flow", [])
        self.assertIn("non-empty list of 'steps'", str(ctx.exception))

    def test_duplicate_step_names_rejected(self) -> None:
        steps = [
            {"name": "step_one", "tool_name": "add_tool", "arguments": {"a": 1, "b": 2}},
            {"name": "step_one", "tool_name": "multiply_tool", "arguments": {"x": 2, "factor": 2}},
        ]
        with self.assertRaises(WorkflowValidationError) as ctx:
            self.manager.validate_workflow_definition("Flow", steps)
        self.assertIn("Duplicate step name 'step_one'", str(ctx.exception))

    def test_unregistered_tool_rejected(self) -> None:
        steps = [
            {"name": "step_bad", "tool_name": "non_existent_tool_xyz", "arguments": {}},
        ]
        with self.assertRaises(WorkflowValidationError) as ctx:
            self.manager.validate_workflow_definition("Flow", steps)
        self.assertIn("references unknown tool", str(ctx.exception))

    def test_cyclic_or_forward_dependency_rejected(self) -> None:
        # step_1 depends on step_2 which hasn't run yet
        steps = [
            {"name": "step_1", "tool_name": "add_tool", "arguments": {"a": 1, "b": 2}, "dependencies": ["step_2"]},
            {"name": "step_2", "tool_name": "multiply_tool", "arguments": {"x": 2, "factor": 2}},
        ]
        with self.assertRaises(WorkflowValidationError) as ctx:
            self.manager.validate_workflow_definition("Flow", steps)
        self.assertIn("Forward references and cycles are forbidden", str(ctx.exception))

    def test_self_dependency_rejected(self) -> None:
        steps = [
            {"name": "step_self", "tool_name": "add_tool", "arguments": {"a": 1, "b": 2}, "dependencies": ["step_self"]},
        ]
        with self.assertRaises(WorkflowValidationError) as ctx:
            self.manager.validate_workflow_definition("Flow", steps)
        self.assertIn("cannot depend on itself", str(ctx.exception))

    def test_invalid_condition_operator_rejected(self) -> None:
        steps = [
            {
                "name": "s1",
                "tool_name": "add_tool",
                "arguments": {"a": 1, "b": 2},
                "condition": {"field": "status", "operator": "invalid_op_xyz", "value": 1},
            }
        ]
        with self.assertRaises(WorkflowValidationError) as ctx:
            self.manager.validate_workflow_definition("Flow", steps)
        self.assertIn("condition operator 'invalid_op_xyz' is unsupported", str(ctx.exception))


class TestConditionEvaluation(unittest.TestCase):
    """Test dynamic condition operator resolution."""

    def test_operators(self) -> None:
        ctx = {
            "step_search": {"output": {"count": 5, "items": ["a", "b"]}},
            "user_status": "active",
            "is_ready": True,
        }

        # gt
        self.assertTrue(evaluate_condition({"field": "step_search.output.count", "operator": "gt", "value": 3}, ctx))
        self.assertFalse(evaluate_condition({"field": "step_search.output.count", "operator": "gt", "value": 10}, ctx))

        # eq
        self.assertTrue(evaluate_condition({"field": "user_status", "operator": "eq", "value": "active"}, ctx))
        self.assertFalse(evaluate_condition({"field": "user_status", "operator": "eq", "value": "inactive"}, ctx))

        # in
        self.assertTrue(evaluate_condition({"field": "user_status", "operator": "in", "value": ["active", "pending"]}, ctx))

        # contains
        self.assertTrue(evaluate_condition({"field": "step_search.output.items", "operator": "contains", "value": "a"}, ctx))
        self.assertFalse(evaluate_condition({"field": "step_search.output.items", "operator": "contains", "value": "z"}, ctx))

        # exists
        self.assertTrue(evaluate_condition({"field": "step_search.output.count", "operator": "exists"}, ctx))
        self.assertFalse(evaluate_condition({"field": "nonexistent.field", "operator": "exists"}, ctx))

        # truthy / falsy
        self.assertTrue(evaluate_condition({"field": "is_ready", "operator": "truthy"}, ctx))
        self.assertFalse(evaluate_condition({"field": "is_ready", "operator": "falsy"}, ctx))


class TestWorkflowExecutionAndPersistence(unittest.IsolatedAsyncioTestCase):
    """Integration test suite executing multi-step workflows with real in-memory SQLite database."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        self.registry = ToolRegistry()
        self.registry.register_tool(AddNumbersMockTool())
        self.registry.register_tool(MultiplyNumbersMockTool())
        self.failing_tool = FailingMockTool(fail_times=1)
        self.registry.register_tool(self.failing_tool)

        self.executor = ToolExecutor(self.registry)
        self.manager = WorkflowManager(self.session_factory, tool_registry=self.registry)
        self.engine_wf = WorkflowEngine(
            session_factory=self.session_factory,
            tool_executor=self.executor,
            workflow_manager=self.manager,
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_sequential_execution_and_output_passing(self) -> None:
        """Verify sequential steps pass outputs to subsequent steps and persist in DB."""
        steps = [
            {"name": "step_add", "tool_name": "add_tool", "arguments": {"a": 10, "b": 15}},
            {"name": "step_mult", "tool_name": "multiply_tool", "arguments": {"x": "{step_add.sum}", "factor": 2}, "dependencies": ["step_add"]},
        ]

        created = await self.engine_wf.create_workflow(
            user_id=101,
            name="Math Workflow",
            steps=steps,
            description="Adds then multiplies",
        )
        wf_id = created["workflow_id"]
        self.assertEqual(created["status"], "pending")

        # Execute workflow
        res = await self.engine_wf.execute_workflow(workflow_id=wf_id, user_id=101)
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["current_step_index"], 2)
        self.assertEqual(res["context_data"]["step_add"]["sum"], 25)
        self.assertEqual(res["context_data"]["step_mult"]["product"], 50)

        # Inspect database records
        status = await self.engine_wf.get_status(wf_id, user_id=101)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(len(status["execution_history"]), 2)
        self.assertEqual(status["execution_history"][0]["status"], "completed")
        self.assertEqual(status["execution_history"][1]["status"], "completed")

    async def test_conditional_step_skipping(self) -> None:
        """Verify step condition evaluating to False is skipped and downstream dependency also skips."""
        steps = [
            {"name": "step_add", "tool_name": "add_tool", "arguments": {"a": 2, "b": 3}},
            # Condition requires sum > 50 (which is False since sum=5)
            {
                "name": "step_conditional",
                "tool_name": "multiply_tool",
                "arguments": {"x": "{step_add.sum}", "factor": 10},
                "condition": {"field": "step_add.sum", "operator": "gt", "value": 50},
                "dependencies": ["step_add"],
            },
            # Downstream step dependent on step_conditional
            {
                "name": "step_downstream",
                "tool_name": "add_tool",
                "arguments": {"a": 1, "b": 1},
                "dependencies": ["step_conditional"],
            },
        ]

        created = await self.engine_wf.create_workflow(user_id=101, name="Conditional Flow", steps=steps)
        wf_id = created["workflow_id"]

        res = await self.engine_wf.execute_workflow(workflow_id=wf_id, user_id=101)
        self.assertEqual(res["status"], "completed")

        status = await self.engine_wf.get_status(wf_id, user_id=101)
        history = status["execution_history"]
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]["status"], "completed")
        self.assertEqual(history[1]["status"], "skipped")  # Condition evaluated to False
        self.assertEqual(history[2]["status"], "skipped")  # Prerequisite was skipped

    async def test_step_retry_recovery(self) -> None:
        """Verify step retries upon failure and succeeds when tool recovers."""
        steps = [
            {"name": "step_fail", "tool_name": "failing_tool", "arguments": {}, "max_retries": 2},
        ]
        created = await self.engine_wf.create_workflow(user_id=101, name="Retry Flow", steps=steps)
        wf_id = created["workflow_id"]

        # Tool fails on attempt 1, succeeds on attempt 2
        res = await self.engine_wf.execute_workflow(workflow_id=wf_id, user_id=101)
        self.assertEqual(res["status"], "completed")
        self.assertTrue(res["context_data"]["step_fail"]["recovered"])

    async def test_unrecoverable_failure(self) -> None:
        """Verify workflow marks status as failed when max retries exceeded."""
        always_fail = FailingMockTool(fail_times=99)
        self.registry.register_tool(always_fail)

        steps = [
            {"name": "step_fatal", "tool_name": "failing_tool", "arguments": {}, "max_retries": 1},
        ]
        created = await self.engine_wf.create_workflow(user_id=101, name="Fatal Flow", steps=steps)
        wf_id = created["workflow_id"]

        res = await self.engine_wf.execute_workflow(workflow_id=wf_id, user_id=101)
        self.assertEqual(res["status"], "failed")
        self.assertIn("failed after 2 attempt(s)", res["error"])

        status = await self.engine_wf.get_status(wf_id, user_id=101)
        self.assertEqual(status["status"], "failed")

    async def test_concurrent_workflow_run_isolation(self) -> None:
        """Verify multiple workflow runs execute with isolated context and state."""
        steps1 = [{"name": "s1", "tool_name": "add_tool", "arguments": {"a": 100, "b": 200}}]
        steps2 = [{"name": "s1", "tool_name": "add_tool", "arguments": {"a": 1, "b": 2}}]

        wf1 = await self.engine_wf.create_workflow(user_id=101, name="Run 1", steps=steps1)
        wf2 = await self.engine_wf.create_workflow(user_id=102, name="Run 2", steps=steps2)

        # Execute concurrently
        r1, r2 = await asyncio.gather(
            self.engine_wf.execute_workflow(wf1["workflow_id"], user_id=101),
            self.engine_wf.execute_workflow(wf2["workflow_id"], user_id=102),
        )

        self.assertEqual(r1["context_data"]["s1"]["sum"], 300)
        self.assertEqual(r2["context_data"]["s1"]["sum"], 3)
        self.assertNotEqual(r1["run_id"], r2["run_id"])


class TestWorkflowPlannerAgent(unittest.IsolatedAsyncioTestCase):
    """Test AIagent natural language planning and conversion to validated workflows."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.registry = ToolRegistry()
        self.registry.register_tool(AddNumbersMockTool())
        self.registry.register_tool(MultiplyNumbersMockTool())

        self.executor = ToolExecutor(self.registry)
        self.manager = WorkflowManager(self.session_factory, tool_registry=self.registry)
        self.engine_wf = WorkflowEngine(self.session_factory, self.executor, self.manager)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_natural_language_to_workflow_plan_and_execution(self) -> None:
        """Verify LLM-generated plan is validated, persisted, and successfully executed."""
        mock_gemini_json = json.dumps({
            "name": "Calculate Total Score",
            "description": "Add base points and multiply by difficulty",
            "steps": [
                {"name": "step_add", "tool_name": "add_tool", "arguments": {"a": 50, "b": 50}},
                {"name": "step_scale", "tool_name": "multiply_tool", "arguments": {"x": "{step_add.sum}", "factor": 3}, "dependencies": ["step_add"]},
            ],
        })

        mock_llm = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.content = f"```json\n{mock_gemini_json}\n```"
        mock_llm.generate.return_value = mock_resp

        planner = WorkflowPlanner(
            workflow_manager=self.manager,
            workflow_engine=self.engine_wf,
            llm_manager=mock_llm,
        )

        # Plan and execute from natural language
        result = await planner.plan_and_execute(user_id=101, natural_language_request="Add 50 and 50 then triple it")
        self.assertEqual(result["name"], "Calculate Total Score")
        self.assertEqual(result["execution"]["status"], "completed")
        self.assertEqual(result["execution"]["context_data"]["step_scale"]["product"], 300)

    async def test_llm_hallucinated_tool_rejected(self) -> None:
        """Verify plan fails validation if LLM invents an unregistered tool."""
        mock_gemini_json = json.dumps({
            "name": "Malicious Plan",
            "steps": [
                {"name": "step_hack", "tool_name": "unregistered_dangerous_tool", "arguments": {}},
            ],
        })

        mock_llm = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.content = mock_gemini_json
        mock_llm.generate.return_value = mock_resp

        planner = WorkflowPlanner(
            workflow_manager=self.manager,
            workflow_engine=self.engine_wf,
            llm_manager=mock_llm,
        )

        with self.assertRaises(WorkflowValidationError) as ctx:
            await planner.plan_workflow(user_id=101, natural_language_request="Do dangerous action")
        self.assertIn("references unknown tool", str(ctx.exception))


class TestTelegramWorkflowIntegration(unittest.IsolatedAsyncioTestCase):
    """Test Telegram /workflows and /workflow commands."""

    def setUp(self) -> None:
        self.user = User(id=123, is_bot=False, first_name="TestUser")
        self.chat = Chat(id=123, type="private")
        self.message = AsyncMock(spec=Message)
        self.update = MagicMock(spec=Update)
        self.update.effective_user = self.user
        self.update.effective_chat = self.chat
        self.update.message = self.message

        self.context = MagicMock()
        self.context.bot_data = {
            "config": MagicMock(allowed_telegram_user_ids=frozenset({123})),
        }

    async def test_workflows_list_command(self) -> None:
        mock_wf_engine = AsyncMock()
        mock_wf_engine.list_workflows.return_value = [
            {"workflow_id": "wf_123", "name": "Sync Task", "status": "running", "current_step_index": 1}
        ]
        self.context.bot_data["workflow_engine"] = mock_wf_engine
        self.context.args = []

        await workflows_command(self.update, self.context)
        self.message.reply_text.assert_awaited_once()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Active Workflows", text)
        self.assertIn("wf_123", text)

    async def test_workflows_status_command(self) -> None:
        mock_wf_engine = AsyncMock()
        mock_wf_engine.get_status.return_value = {
            "workflow_id": "wf_123",
            "name": "Sync Task",
            "status": "completed",
            "steps": [
                {"name": "fetch", "tool_name": "web_search", "status": "completed"},
            ],
        }
        self.context.bot_data["workflow_engine"] = mock_wf_engine
        self.context.args = ["status", "wf_123"]

        await workflows_command(self.update, self.context)
        self.message.reply_text.assert_awaited_once()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("Workflow Details: Sync Task", text)
        self.assertIn("`fetch`", text)

    async def test_workflows_cancel_command(self) -> None:
        mock_wf_engine = AsyncMock()
        mock_wf_engine.cancel_workflow.return_value = True
        self.context.bot_data["workflow_engine"] = mock_wf_engine
        self.context.args = ["cancel", "wf_123"]

        await workflows_command(self.update, self.context)
        self.message.reply_text.assert_awaited_once()
        text = self.message.reply_text.call_args[0][0]
        self.assertIn("cancelled successfully", text)


if __name__ == "__main__":
    unittest.main()
