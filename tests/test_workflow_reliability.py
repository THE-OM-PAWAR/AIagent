"""Comprehensive test suite for Workflow Reliability, Concurrency Leases, Retries, and Approval Management."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.approval_manager import (
    ApprovalExpiredError,
    ApprovalInvalidStateError,
    ApprovalManager,
    ApprovalNotFoundError,
    ApprovalUnauthorizedError,
)
from src.core.workflow_engine import (
    WorkflowEngine,
    compute_backoff_delay,
    is_retryable_error,
)
from src.core.workflow_manager import WorkflowManager
from src.database.models import Base
from src.database.models.workflow import (
    ApprovalStatus,
    WorkflowModel,
    WorkflowRunModel,
    WorkflowState,
    validate_workflow_state_transition,
)
from src.database.repositories.workflow_repo import (
    WorkflowConcurrencyError,
    WorkflowRepository,
)
from src.tools.base import BaseTool
from src.tools.executor import ToolExecutor
from src.tools.permissions import PermissionPolicy, ToolPermission
from src.tools.registry import ToolRegistry


class SensitiveExternalMockTool(BaseTool):
    """Tool simulating a consequential external action requiring human approval."""

    def __init__(self) -> None:
        self.execution_count = 0

    @property
    def name(self) -> str:
        return "send_external_email"

    @property
    def description(self) -> str:
        return "Send external email to client"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "subject": {"type": "string"},
            },
            "required": ["recipient", "subject"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def requires_approval(self) -> bool:
        return True

    async def execute(self, recipient: str, subject: str, **kwargs) -> dict:
        self.execution_count += 1
        return {"status": "sent", "recipient": recipient, "message_id": f"msg_{self.execution_count}"}


class SimpleStepMockTool(BaseTool):
    """Safe idempotent read-only tool."""

    def __init__(self) -> None:
        self.execution_count = 0

    @property
    def name(self) -> str:
        return "data_fetcher"

    @property
    def description(self) -> str:
        return "Fetch data"

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {"val": {"type": "integer"}}}

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, val: int = 1, **kwargs) -> dict:
        self.execution_count += 1
        return {"fetched_val": val, "call": self.execution_count}


class FlakyNetworkMockTool(BaseTool):
    """Tool that fails with transient network errors before succeeding."""

    def __init__(self, fail_count: int = 2) -> None:
        self.fail_count = fail_count
        self.calls = 0

    @property
    def name(self) -> str:
        return "flaky_network_tool"

    @property
    def description(self) -> str:
        return "Flaky network simulation"

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object"}

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, **kwargs) -> dict:
        self.calls += 1
        if self.calls <= self.fail_count:
            raise ConnectionResetError("Remote server closed connection unexpectedly (503 Service Unavailable)")
        return {"network_data": "success", "attempts_needed": self.calls}


class TestWorkflowStateTransitions(unittest.TestCase):
    """Test state machine validity rules and transition enforcement."""

    def test_valid_transitions(self) -> None:
        # draft -> pending
        validate_workflow_state_transition(WorkflowState.DRAFT.value, WorkflowState.PENDING.value)
        # pending -> running
        validate_workflow_state_transition(WorkflowState.PENDING.value, WorkflowState.RUNNING.value)
        # running -> waiting_for_approval
        validate_workflow_state_transition(WorkflowState.RUNNING.value, WorkflowState.WAITING_FOR_APPROVAL.value)
        # waiting_for_approval -> running
        validate_workflow_state_transition(WorkflowState.WAITING_FOR_APPROVAL.value, WorkflowState.RUNNING.value)
        # running -> paused
        validate_workflow_state_transition(WorkflowState.RUNNING.value, WorkflowState.PAUSED.value)
        # paused -> running
        validate_workflow_state_transition(WorkflowState.PAUSED.value, WorkflowState.RUNNING.value)
        # running -> completed
        validate_workflow_state_transition(WorkflowState.RUNNING.value, WorkflowState.COMPLETED.value)
        # running -> failed
        validate_workflow_state_transition(WorkflowState.RUNNING.value, WorkflowState.FAILED.value)
        # failed -> running (retry from checkpoint)
        validate_workflow_state_transition(WorkflowState.FAILED.value, WorkflowState.RUNNING.value)
        # running -> cancelled
        validate_workflow_state_transition(WorkflowState.RUNNING.value, WorkflowState.CANCELLED.value)

    def test_forbidden_transitions(self) -> None:
        # Terminal completed cannot transition
        with self.assertRaises(ValueError) as ctx:
            validate_workflow_state_transition(WorkflowState.COMPLETED.value, WorkflowState.RUNNING.value)
        self.assertIn("Invalid workflow state transition", str(ctx.exception))

        # Terminal cancelled cannot transition
        with self.assertRaises(ValueError):
            validate_workflow_state_transition(WorkflowState.CANCELLED.value, WorkflowState.PENDING.value)

        # draft cannot jump to completed
        with self.assertRaises(ValueError):
            validate_workflow_state_transition(WorkflowState.DRAFT.value, WorkflowState.COMPLETED.value)

        # pending cannot jump to waiting_for_approval
        with self.assertRaises(ValueError):
            validate_workflow_state_transition(WorkflowState.PENDING.value, WorkflowState.WAITING_FOR_APPROVAL.value)


class TestRetryAndFailureClassification(unittest.TestCase):
    """Test retryable error classification and exponential backoff calculations."""

    def test_retryable_classification(self) -> None:
        self.assertTrue(is_retryable_error(TimeoutError("Request timed out")))
        self.assertTrue(is_retryable_error(ConnectionError("Connection refused by host")))
        self.assertTrue(is_retryable_error("HTTP 429: Too Many Requests / Rate limit exceeded"))
        self.assertTrue(is_retryable_error("503 Service Unavailable: temporary backend glitch"))

        # Non-retryable
        self.assertFalse(is_retryable_error("Tool requires human-in-the-loop approval before execution"))
        self.assertFalse(is_retryable_error("Permission denied: user lacks required privileges"))
        self.assertFalse(is_retryable_error("WorkflowValidationError: duplicate step names"))
        self.assertFalse(is_retryable_error("ZeroDivisionError: division by zero"))

    def test_backoff_delay_calculation(self) -> None:
        d1 = compute_backoff_delay(attempt=1, initial_delay=1.0, backoff_factor=2.0, max_delay=10.0, jitter=0.0)
        self.assertEqual(d1, 1.0)

        d2 = compute_backoff_delay(attempt=2, initial_delay=1.0, backoff_factor=2.0, max_delay=10.0, jitter=0.0)
        self.assertEqual(d2, 2.0)

        d3 = compute_backoff_delay(attempt=3, initial_delay=1.0, backoff_factor=2.0, max_delay=10.0, jitter=0.0)
        self.assertEqual(d3, 4.0)

        # Respects max_delay
        d10 = compute_backoff_delay(attempt=10, initial_delay=1.0, backoff_factor=2.0, max_delay=10.0, jitter=0.0)
        self.assertEqual(d10, 10.0)


class TestApprovalManager(unittest.IsolatedAsyncioTestCase):
    """Test human approval requests, authorization checks, expiration, and resolution."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.appr_mgr = ApprovalManager(self.session_factory, default_ttl_hours=1)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_create_and_approve_request(self) -> None:
        req = await self.appr_mgr.create_approval_request(
            workflow_id="wf_1",
            run_id="run_1",
            step_name="step_email",
            tool_name="send_external_email",
            action_summary="Send email to user@example.com",
            action_arguments={"recipient": "user@example.com", "subject": "Hello"},
            requested_by_user_id=101,
            ttl_seconds=3600,
        )
        appr_id = req["approval_id"]
        self.assertEqual(req["status"], "pending")

        # Approve by owner
        approved = await self.appr_mgr.approve_request(appr_id, user_id=101, resolution_notes="Verified by owner")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["resolved_by_user_id"], 101)

        # Resolving already resolved request raises error
        with self.assertRaises(ApprovalInvalidStateError):
            await self.appr_mgr.approve_request(appr_id, user_id=101)

    async def test_unauthorized_user_cannot_approve(self) -> None:
        req = await self.appr_mgr.create_approval_request(
            workflow_id="wf_1",
            run_id="run_1",
            step_name="step_email",
            tool_name="send_external_email",
            action_summary="Send email",
            action_arguments={},
            requested_by_user_id=101,
        )
        appr_id = req["approval_id"]

        # Different user (not admin) is rejected
        with self.assertRaises(ApprovalUnauthorizedError):
            await self.appr_mgr.approve_request(appr_id, user_id=999, is_admin=False)

    async def test_expired_approval_request(self) -> None:
        req = await self.appr_mgr.create_approval_request(
            workflow_id="wf_1",
            run_id="run_1",
            step_name="step_email",
            tool_name="send_external_email",
            action_summary="Send email",
            action_arguments={},
            requested_by_user_id=101,
            ttl_seconds=-10,  # Already expired in the past
        )
        appr_id = req["approval_id"]

        with self.assertRaises(ApprovalExpiredError):
            await self.appr_mgr.approve_request(appr_id, user_id=101)


class TestWorkflowReliabilityAndExecution(unittest.IsolatedAsyncioTestCase):
    """Integration test suite executing workflows with leases, restart recovery, and approvals."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        self.registry = ToolRegistry()
        self.email_tool = SensitiveExternalMockTool()
        self.data_tool = SimpleStepMockTool()
        self.flaky_tool = FlakyNetworkMockTool(fail_count=1)

        self.registry.register_tool(self.email_tool)
        self.registry.register_tool(self.data_tool)
        self.registry.register_tool(self.flaky_tool)

        self.policy = PermissionPolicy()
        self.policy.requires_approval_tools.add("send_external_email")

        self.executor = ToolExecutor(self.registry, permission_policy=self.policy)
        self.appr_mgr = ApprovalManager(self.session_factory)
        self.manager = WorkflowManager(self.session_factory, tool_registry=self.registry, permission_policy=self.policy)
        self.wf_engine = WorkflowEngine(
            session_factory=self.session_factory,
            tool_executor=self.executor,
            workflow_manager=self.manager,
            approval_manager=self.appr_mgr,
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_concurrency_lease_prevents_duplicate_workers(self) -> None:
        """Verify worker lease blocks concurrent worker from executing the same run."""
        steps = [{"name": "s1", "tool_name": "data_fetcher", "arguments": {"val": 42}}]
        wf = await self.wf_engine.create_workflow(user_id=101, name="Lease Test", steps=steps)
        wf_id = wf["workflow_id"]

        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            run = await repo.create_run(wf_id, user_id=101)
            run_id = run.id

            # Worker 1 acquires lease
            acquired1 = await repo.acquire_execution_lease(run_id, worker_token="worker_alpha", lease_seconds=60)
            self.assertTrue(acquired1)

            # Worker 2 attempts to acquire active lease -> fails
            acquired2 = await repo.acquire_execution_lease(run_id, worker_token="worker_beta", lease_seconds=60)
            self.assertFalse(acquired2)

            # Worker 1 finishes and releases lease
            released = await repo.release_execution_lease(run_id, worker_token="worker_alpha")
            self.assertTrue(released)

            # Worker 2 can now acquire lease
            acquired3 = await repo.acquire_execution_lease(run_id, worker_token="worker_beta", lease_seconds=60)
            self.assertTrue(acquired3)

    async def test_application_restart_recovery(self) -> None:
        """Verify startup crash recovery transitions running workflows to paused safely."""
        steps = [{"name": "s1", "tool_name": "data_fetcher", "arguments": {"val": 10}}]
        wf = await self.wf_engine.create_workflow(user_id=101, name="Crashed Flow", steps=steps)
        wf_id = wf["workflow_id"]

        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            await repo.update_status(wf_id, WorkflowState.RUNNING.value)
            run = await repo.create_run(wf_id, user_id=101)
            await repo.update_run_status(run.id, WorkflowState.RUNNING.value)

        # System reboots
        recovered_count = await self.wf_engine.recover_on_startup()
        self.assertGreaterEqual(recovered_count, 1)

        # Check status is now safely paused
        status = await self.wf_engine.get_status(wf_id, user_id=101)
        self.assertEqual(status["status"], WorkflowState.PAUSED.value)
        self.assertIn("safely paused without duplicate execution", status["error_message"])

    async def test_human_approval_pause_and_resumption(self) -> None:
        """Verify workflow safely yields when sensitive tool needs approval and resumes cleanly on grant."""
        steps = [
            {"name": "fetch_step", "tool_name": "data_fetcher", "arguments": {"val": 99}},
            {"name": "send_step", "tool_name": "send_external_email", "arguments": {"recipient": "client@example.com", "subject": "Notice"}, "dependencies": ["fetch_step"]},
        ]

        wf = await self.wf_engine.create_workflow(user_id=101, name="Approval Flow", steps=steps)
        wf_id = wf["workflow_id"]

        # Run 1: Step 1 completes, Step 2 pauses for human approval
        res1 = await self.wf_engine.execute_workflow(wf_id, user_id=101)
        self.assertEqual(res1["status"], WorkflowState.WAITING_FOR_APPROVAL.value)
        self.assertEqual(res1["step_name"], "send_step")
        approval_id = res1["approval_id"]

        # Sensitive tool must NOT have been executed yet
        self.assertEqual(self.email_tool.execution_count, 0)
        self.assertEqual(self.data_tool.execution_count, 1)

        # Inspect pending approval via ApprovalManager
        pending_list = await self.appr_mgr.list_pending_approvals(user_id=101)
        self.assertEqual(len(pending_list), 1)
        self.assertEqual(pending_list[0]["approval_id"], approval_id)

        # User grants approval
        await self.appr_mgr.approve_request(approval_id, user_id=101, resolution_notes="Authorized by supervisor")

        # Resume execution
        resume_res = await self.wf_engine.resume_approved_workflow(approval_id=approval_id, user_id=101)
        self.assertEqual(resume_res["status"], WorkflowState.COMPLETED.value)

        # Sensitive tool executed exactly once
        self.assertEqual(self.email_tool.execution_count, 1)
        # Previous step 1 was NOT re-executed
        self.assertEqual(self.data_tool.execution_count, 1)

    async def test_never_repeat_completed_external_actions_on_resume(self) -> None:
        """Verify that resuming an interrupted or failed workflow reuses previous step output."""
        steps = [
            {"name": "safe_fetch", "tool_name": "data_fetcher", "arguments": {"val": 1}},
            {"name": "flaky_step", "tool_name": "flaky_network_tool", "arguments": {}, "max_retries": 2, "dependencies": ["safe_fetch"]},
        ]
        wf = await self.wf_engine.create_workflow(user_id=101, name="Idempotent Resume Flow", steps=steps)
        wf_id = wf["workflow_id"]

        res = await self.wf_engine.execute_workflow(wf_id, user_id=101)
        self.assertEqual(res["status"], "completed")

        # Fetch step ran once
        self.assertEqual(self.data_tool.execution_count, 1)

        # Calling execute_workflow again on completed workflow is rejected
        with self.assertRaises(ValueError) as ctx:
            await self.wf_engine.execute_workflow(wf_id, user_id=101)
        self.assertIn("Cannot execute workflow in terminal state 'completed'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

