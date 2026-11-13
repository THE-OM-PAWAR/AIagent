"""Comprehensive test suite for Recurring Workflow Lifecycle, Execution Management, Overlap Prevention, and Policies."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.approval_manager import ApprovalManager
from src.core.triggers import IntervalTrigger, _as_utc
from src.core.workflow_engine import WorkflowEngine
from src.core.workflow_manager import WorkflowManager
from src.core.workflow_scheduler import WorkflowScheduler
from src.database.models import Base
from src.database.models.workflow import (
    ApprovalStatus,
    CatchUpPolicy,
    ScheduleStatus,
    WorkflowModel,
    WorkflowRunModel,
    WorkflowScheduleModel,
    WorkflowState,
)
from src.database.repositories.schedule_repo import ScheduleRepository
from src.database.repositories.workflow_repo import WorkflowRepository
from src.tools.base import BaseTool
from src.tools.executor import ToolExecutor
from src.tools.permissions import PermissionPolicy
from src.tools.registry import ToolRegistry


class CountingStepTool(BaseTool):
    """Tool that records execution count and can be configured to fail or succeed."""

    def __init__(self, name: str = "counting_tool") -> None:
        self._name = name
        self.call_count = 0
        self.should_fail = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Counting tool for recurring workflow lifecycle testing."

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
            },
        }

    async def execute(self, **kwargs) -> dict:
        self.call_count += 1
        if self.should_fail:
            raise RuntimeError(f"Simulated execution failure on call {self.call_count}")
        return {"result": f"processed call {self.call_count}", "message": kwargs.get("message", "ok")}


class SensitiveApprovalStepTool(BaseTool):
    """Tool requiring approval before executing."""

    def __init__(self, name: str = "recurring_sensitive_tool") -> None:
        self._name = name
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Sensitive tool requiring authorization."

    @property
    def requires_approval(self) -> bool:
        return True

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
            },
        }

    async def execute(self, **kwargs) -> dict:
        self.call_count += 1
        return {"status": "authorized_and_executed", "action": kwargs.get("action")}


class TestRecurringWorkflowLifecycle(unittest.IsolatedAsyncioTestCase):
    """Comprehensive tests for recurring workflow lifecycle, scheduled runs, and execution policies."""

    async def asyncSetUp(self) -> None:
        """Initialize in-memory SQLite database and test harness."""
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        self.tool_registry = ToolRegistry()
        self.counting_tool = CountingStepTool()
        self.sensitive_tool = SensitiveApprovalStepTool()
        self.tool_registry.register_tool(self.counting_tool)
        self.tool_registry.register_tool(self.sensitive_tool)

        self.permission_policy = PermissionPolicy(
            allowed_tools=["counting_tool", "recurring_sensitive_tool"],
            require_approval_tools=["recurring_sensitive_tool"],
        )
        self.executor = ToolExecutor(self.tool_registry, permission_policy=self.permission_policy)

        self.manager = WorkflowManager(
            session_factory=self.session_factory,
            tool_registry=self.tool_registry,
            permission_policy=self.permission_policy,
        )

        self.workflow_engine = WorkflowEngine(
            session_factory=self.session_factory,
            tool_executor=self.executor,
            workflow_manager=self.manager,
        )

        self.notifications: list[dict] = []

        async def _notification_handler(user_id: int, message: str) -> None:
            self.notifications.append({"user_id": user_id, "message": message})

        self.scheduler = WorkflowScheduler(
            session_factory=self.session_factory,
            workflow_engine=self.workflow_engine,
            poll_interval_seconds=0.1,
            batch_size=10,
            lease_seconds=30,
            worker_id="recurring_worker_1",
            notification_handler=_notification_handler,
        )

    async def asyncTearDown(self) -> None:
        """Clean shutdown of scheduler and database engine."""
        if self.scheduler.is_running:
            await self.scheduler.stop()
        await self.engine.dispose()

    # -------------------------------------------------------------------------
    # 1. Multiple Scheduled Occurrences & Independent Run History
    # -------------------------------------------------------------------------

    async def test_multiple_scheduled_occurrences_independent_runs(self) -> None:
        """Verify recurring workflow generates distinct runs with isolated run histories and timestamps."""
        # Create reusable recurring workflow definition (is_one_time=False)
        wf_dict = await self.manager.create_workflow(
            user_id=1001,
            name="Daily Data Sync",
            steps=[{"name": "sync_step", "tool_name": "counting_tool", "arguments": {"message": "batch"}}],
            is_one_time=False,
        )
        wf_id = wf_dict["workflow_id"]

        # Create schedule
        schedule = await self.scheduler.schedule_workflow(
            workflow_id=wf_id,
            user_id=1001,
            name="Sync Every Minute",
            trigger_type="interval",
            trigger_config={"interval_seconds": 60},
        )

        now = datetime.now(timezone.utc)

        # Occurrence 1
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.next_run_at = now - timedelta(seconds=10)
            await session.commit()

        results_1 = await self.scheduler._poll_once()
        self.assertEqual(len(results_1), 1)
        self.assertIn(results_1[0]["status"], ("success", "completed"))
        run_id_1 = results_1[0]["run_result"]["run_id"]

        # Occurrence 2 (distinct occurrence timestamp, also due)
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.next_run_at = now - timedelta(seconds=5)
            await session.commit()

        results_2 = await self.scheduler._poll_once()
        self.assertEqual(len(results_2), 1)
        self.assertIn(results_2[0]["status"], ("success", "completed"))
        run_id_2 = results_2[0]["run_result"]["run_id"]

        # Assert independent runs were created
        self.assertNotEqual(run_id_1, run_id_2)

        async with self.session_factory() as session:
            wf_repo = WorkflowRepository(session)
            run1 = await wf_repo.get_run(run_id_1)
            run2 = await wf_repo.get_run(run_id_2)

            self.assertEqual(run1.status, WorkflowState.COMPLETED.value)
            self.assertEqual(run2.status, WorkflowState.COMPLETED.value)
            self.assertEqual(run1.schedule_id, schedule.id)
            self.assertEqual(run2.schedule_id, schedule.id)

            # Workflow definition remained PENDING/active so further occurrences can run
            parent_wf = await wf_repo.get_workflow(wf_id)
            self.assertEqual(parent_wf.status, WorkflowState.PENDING.value)
            self.assertFalse(parent_wf.is_one_time)

            # Tool was called exactly twice (once per occurrence)
            self.assertEqual(self.counting_tool.call_count, 2)

    # -------------------------------------------------------------------------
    # 2. Failed Occurrence Followed by Successful Occurrence
    # -------------------------------------------------------------------------

    async def test_failed_occurrence_followed_by_success(self) -> None:
        """Verify a failed occurrence does not corrupt future occurrences or lock the workflow."""
        wf_dict = await self.manager.create_workflow(
            user_id=1002,
            name="Resilient Recurring Task",
            steps=[{"name": "step1", "tool_name": "counting_tool", "arguments": {}}],
            is_one_time=False,
        )
        wf_id = wf_dict["workflow_id"]

        schedule = await self.scheduler.schedule_workflow(
            workflow_id=wf_id,
            user_id=1002,
            name="Resilient Schedule",
            trigger_type="interval",
            trigger_config={"interval_seconds": 60},
        )

        now = datetime.now(timezone.utc)

        # Make Occurrence 1 fail
        self.counting_tool.should_fail = True
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.next_run_at = now - timedelta(seconds=1)
            await session.commit()

        results_1 = await self.scheduler._poll_once()
        self.assertEqual(len(results_1), 1)
        self.assertEqual(results_1[0]["status"], "failed")

        # Verify consecutive failures incremented
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            self.assertEqual(sch.consecutive_failures, 1)

        # Make Occurrence 2 succeed
        self.counting_tool.should_fail = False
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.next_run_at = now - timedelta(seconds=1)
            await session.commit()

        results_2 = await self.scheduler._poll_once()
        self.assertEqual(len(results_2), 1)
        self.assertIn(results_2[0]["status"], ("success", "completed"))

        # Verify consecutive failures was reset to 0
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            self.assertEqual(sch.consecutive_failures, 0)
            self.assertEqual(sch.total_runs_count, 2)

    # -------------------------------------------------------------------------
    # 3. Overlapping Execution Prevention (allow_overlap=False)
    # -------------------------------------------------------------------------

    async def test_overlapping_execution_prevention(self) -> None:
        """Verify scheduler skips or defers occurrence when an active run is in progress and allow_overlap=False."""
        wf_dict = await self.manager.create_workflow(
            user_id=1003,
            name="Non-Overlapping Job",
            steps=[{"name": "step1", "tool_name": "counting_tool", "arguments": {}}],
            is_one_time=False,
        )
        wf_id = wf_dict["workflow_id"]

        schedule = await self.scheduler.schedule_workflow(
            workflow_id=wf_id,
            user_id=1003,
            name="No Overlap Schedule",
            trigger_type="interval",
            trigger_config={"interval_seconds": 60},
            allow_overlap=False,
            max_concurrency=1,
        )

        now = datetime.now(timezone.utc)

        # Create an active in-progress run for this schedule
        async with self.session_factory() as session:
            wf_repo = WorkflowRepository(session)
            active_run = await wf_repo.create_run(
                workflow_id=wf_id,
                user_id=1003,
                schedule_id=schedule.id,
                trigger_type="scheduler",
            )
            await wf_repo.update_run_status(active_run.id, WorkflowState.RUNNING.value)

            # Make schedule due
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.next_run_at = now - timedelta(seconds=1)
            await session.commit()

        # Poll loop executes: should detect active run and defer without launching a new one
        initial_calls = self.counting_tool.call_count
        results = await self.scheduler._poll_once()

        # Dispatched results is empty because occurrence was deferred
        self.assertEqual(len(results), 0)
        self.assertEqual(self.counting_tool.call_count, initial_calls)

        # Verify next_run_at was advanced into the future
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            self.assertGreater(_as_utc(sch.next_run_at), now)

    # -------------------------------------------------------------------------
    # 4. Auto-Pause on Max Consecutive Failures
    # -------------------------------------------------------------------------

    async def test_auto_pause_on_max_consecutive_failures(self) -> None:
        """Verify schedule is auto-paused when max_consecutive_failures threshold is reached."""
        wf_dict = await self.manager.create_workflow(
            user_id=1004,
            name="Flaky Service Workflow",
            steps=[{"name": "failing_step", "tool_name": "counting_tool", "arguments": {}}],
            is_one_time=False,
        )
        wf_id = wf_dict["workflow_id"]

        # Configure max_consecutive_failures=2
        schedule = await self.scheduler.schedule_workflow(
            workflow_id=wf_id,
            user_id=1004,
            name="Flaky Schedule",
            trigger_type="interval",
            trigger_config={"interval_seconds": 60},
            max_consecutive_failures=2,
        )

        self.counting_tool.should_fail = True
        now = datetime.now(timezone.utc)

        # Failure 1
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.next_run_at = now - timedelta(seconds=1)
            await session.commit()

        await self.scheduler._poll_once()

        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            self.assertEqual(sch.status, ScheduleStatus.ACTIVE.value)
            self.assertEqual(sch.consecutive_failures, 1)

        # Failure 2 (reaches threshold of 2)
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.next_run_at = now - timedelta(seconds=1)
            await session.commit()

        await self.scheduler._poll_once()

        # Should now be auto-paused!
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            self.assertEqual(sch.status, ScheduleStatus.PAUSED.value)
            self.assertEqual(sch.consecutive_failures, 2)

    # -------------------------------------------------------------------------
    # 5. Execution Budget Enforcement
    # -------------------------------------------------------------------------

    async def test_api_call_budget_enforcement(self) -> None:
        """Verify per-run api_call_budget limits tool calls and fails cleanly if exceeded."""
        wf_dict = await self.manager.create_workflow(
            user_id=1005,
            name="Multi Step Workflow",
            steps=[
                {"name": "step1", "tool_name": "counting_tool", "arguments": {}},
                {"name": "step2", "tool_name": "counting_tool", "arguments": {}},
            ],
            is_one_time=False,
        )
        wf_id = wf_dict["workflow_id"]

        # Schedule with budget of only 1 call
        schedule = await self.scheduler.schedule_workflow(
            workflow_id=wf_id,
            user_id=1005,
            name="Budget Constrained Schedule",
            trigger_type="interval",
            trigger_config={"interval_seconds": 60},
            api_call_budget=1,
        )

        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.next_run_at = now - timedelta(seconds=1)
            await session.commit()

        results = await self.scheduler._poll_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("budget", results[0]["run_result"]["error"].lower())

    # -------------------------------------------------------------------------
    # 6. Sensitive Actions & Approval Recovery in Recurring Workflows
    # -------------------------------------------------------------------------

    async def test_recurring_workflow_approval_management(self) -> None:
        """Verify consequential steps enter waiting_for_approval without breaking recurrence."""
        wf_dict = await self.manager.create_workflow(
            user_id=1006,
            name="Approval Requiring Task",
            steps=[{"name": "sensitive_step", "tool_name": "recurring_sensitive_tool", "arguments": {"action": "delete"}}],
            is_one_time=False,
        )
        wf_id = wf_dict["workflow_id"]

        schedule = await self.scheduler.schedule_workflow(
            workflow_id=wf_id,
            user_id=1006,
            name="Approval Schedule",
            trigger_type="interval",
            trigger_config={"interval_seconds": 60},
        )

        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.next_run_at = now - timedelta(seconds=1)
            await session.commit()

        # Poll cycle: executes up to approval requirement
        results = await self.scheduler._poll_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], WorkflowState.WAITING_FOR_APPROVAL.value)

        appr_id = results[0]["run_result"]["approval_id"]
        self.assertIsNotNone(appr_id)

        # Verify notification was sent
        self.assertTrue(any("Approval Required" in n["message"] for n in self.notifications))

        # Approve and resume
        approval_mgr = self.workflow_engine.approval_manager
        await approval_mgr.approve_request(appr_id, user_id=1006)
        resume_res = await self.workflow_engine.resume_approved_workflow(appr_id, user_id=1006)
        self.assertEqual(resume_res["status"], WorkflowState.COMPLETED.value)
        self.assertEqual(self.sensitive_tool.call_count, 1)

    # -------------------------------------------------------------------------
    # 7. User Isolation and Authorization
    # -------------------------------------------------------------------------

    async def test_user_isolation_and_authorization(self) -> None:
        """Verify schedules and runs are strictly isolated across different users."""
        wf_1 = await self.manager.create_workflow(
            user_id=2001,
            name="User 1 Task",
            steps=[{"name": "s1", "tool_name": "counting_tool", "arguments": {}}],
            is_one_time=False,
        )
        sch_1 = await self.scheduler.schedule_workflow(
            workflow_id=wf_1["workflow_id"],
            user_id=2001,
            name="User 1 Schedule",
            trigger_type="interval",
            trigger_config={"interval_seconds": 60},
        )

        # User 2 cannot access or pause User 1's schedule
        paused = await self.scheduler.pause_schedule(schedule_id=sch_1.id, user_id=2002)
        self.assertFalse(paused)

        resumed = await self.scheduler.resume_schedule(schedule_id=sch_1.id, user_id=2002)
        self.assertFalse(resumed)

        cancelled = await self.scheduler.cancel_schedule(schedule_id=sch_1.id, user_id=2002)
        self.assertFalse(cancelled)

        # User 1 can successfully pause
        paused_ok = await self.scheduler.pause_schedule(schedule_id=sch_1.id, user_id=2001)
        self.assertTrue(paused_ok)


if __name__ == "__main__":
    unittest.main()

