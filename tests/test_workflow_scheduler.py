"""Comprehensive test suite for Workflow Scheduler, Recurrence Triggers, Atomic Claiming, and Catch-up Policies."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import zoneinfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.triggers import (
    CronTrigger,
    DailyTrigger,
    IntervalTrigger,
    OneTimeTrigger,
    TriggerValidationError,
    WeeklyTrigger,
    _as_utc,
    create_trigger,
)
from src.core.workflow_engine import WorkflowEngine
from src.core.workflow_manager import WorkflowManager
from src.core.workflow_scheduler import WorkflowScheduler
from src.database.models import Base
from src.database.models.workflow import (
    CatchUpPolicy,
    ScheduleStatus,
    WorkflowModel,
    WorkflowScheduleModel,
    WorkflowState,
)
from src.database.repositories.schedule_repo import ScheduleRepository
from src.database.repositories.workflow_repo import WorkflowRepository
from src.tools.base import BaseTool
from src.tools.executor import ToolExecutor
from src.tools.permissions import PermissionPolicy
from src.tools.registry import ToolRegistry


class DummyStepTool(BaseTool):
    """Dummy tool for testing scheduled workflow execution."""

    def __init__(self, name: str = "dummy_scheduler_tool") -> None:
        self._name = name
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Dummy tool for scheduler tests."

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
        return {"result": f"processed: {kwargs.get('message', 'ok')}", "calls": self.call_count}


class TestWorkflowScheduler(unittest.IsolatedAsyncioTestCase):
    """Comprehensive tests for persistent workflow scheduler and triggers."""

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
        self.dummy_tool = DummyStepTool()
        self.tool_registry.register_tool(self.dummy_tool)

        self.executor = ToolExecutor(self.tool_registry)
        self.permission_policy = PermissionPolicy(allowed_tools=["dummy_scheduler_tool"])

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

        self.scheduler = WorkflowScheduler(
            session_factory=self.session_factory,
            workflow_engine=self.workflow_engine,
            poll_interval_seconds=0.1,
            batch_size=10,
            lease_seconds=30,
            worker_id="test_worker_1",
        )

    async def asyncTearDown(self) -> None:
        """Clean shutdown of scheduler and database engine."""
        if self.scheduler.is_running:
            await self.scheduler.stop()
        await self.engine.dispose()

    # -------------------------------------------------------------------------
    # 1. Trigger Recurrence Tests
    # -------------------------------------------------------------------------

    def test_one_time_trigger(self) -> None:
        """Test one-time immediate and future execution triggers."""
        # Immediate
        imm = OneTimeTrigger(is_immediate=True)
        now = datetime.now(timezone.utc)
        self.assertEqual(imm.compute_next_run(now), None)

        # Future
        future_dt = now + timedelta(hours=2)
        ott = OneTimeTrigger(scheduled_at=future_dt)
        self.assertEqual(ott.compute_next_run(now), future_dt)
        # After scheduled time, returns None
        self.assertEqual(ott.compute_next_run(future_dt + timedelta(seconds=1)), None)

    def test_interval_trigger(self) -> None:
        """Test fixed second interval calculation."""
        it = IntervalTrigger(interval_seconds=300)
        base = datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc)
        nxt = it.compute_next_run(base)
        self.assertEqual(nxt, datetime(2026, 11, 13, 10, 5, 0, tzinfo=timezone.utc))

        with self.assertRaises(TriggerValidationError):
            IntervalTrigger(interval_seconds=0)

    def test_daily_trigger(self) -> None:
        """Test daily recurrence across days and timezone boundaries."""
        dt = DailyTrigger(time_str="09:30")
        # Base before 09:30 -> fires at 09:30 same day
        base_early = datetime(2026, 11, 13, 8, 0, 0, tzinfo=timezone.utc)
        nxt_early = dt.compute_next_run(base_early, tz_str="UTC")
        self.assertEqual(nxt_early, datetime(2026, 11, 13, 9, 30, 0, tzinfo=timezone.utc))

        # Base after 09:30 -> fires at 09:30 next day
        base_late = datetime(2026, 11, 13, 11, 0, 0, tzinfo=timezone.utc)
        nxt_late = dt.compute_next_run(base_late, tz_str="UTC")
        self.assertEqual(nxt_late, datetime(2026, 11, 14, 9, 30, 0, tzinfo=timezone.utc))

    def test_weekly_trigger(self) -> None:
        """Test weekly recurrence on specific days."""
        # 2026-11-13 is Friday (weekday index 4: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6)
        wt = WeeklyTrigger(time_str="14:00", days_of_week=[0, 4])  # Mondays and Fridays

        # Friday before 14:00 -> fires today at 14:00
        base_friday_morning = datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc)
        nxt = wt.compute_next_run(base_friday_morning, tz_str="UTC")
        self.assertEqual(nxt, datetime(2026, 11, 13, 14, 0, 0, tzinfo=timezone.utc))

        # Friday after 14:00 -> fires next Monday (2026-11-16) at 14:00
        base_friday_evening = datetime(2026, 11, 13, 16, 0, 0, tzinfo=timezone.utc)
        nxt_mon = wt.compute_next_run(base_friday_evening, tz_str="UTC")
        self.assertEqual(nxt_mon, datetime(2026, 11, 16, 14, 0, 0, tzinfo=timezone.utc))

    def test_cron_trigger(self) -> None:
        """Test pure-Python 5-field cron parsing and next-run evaluation."""
        # Every 15 minutes: "*/15 * * * *"
        ct = CronTrigger(cron_expression="*/15 * * * *")
        base = datetime(2026, 11, 13, 10, 7, 0, tzinfo=timezone.utc)
        nxt = ct.compute_next_run(base, tz_str="UTC")
        self.assertEqual(nxt, datetime(2026, 11, 13, 10, 15, 0, tzinfo=timezone.utc))

        # Weekdays at 08:30: "30 8 * * 1-5"
        ct_weekday = CronTrigger(cron_expression="30 8 * * 1-5")
        # 2026-11-13 is Friday at 09:00 -> should jump over Saturday/Sunday to Monday 2026-11-16 at 08:30
        nxt_week = ct_weekday.compute_next_run(datetime(2026, 11, 13, 9, 0, 0, tzinfo=timezone.utc), tz_str="UTC")
        self.assertEqual(nxt_week, datetime(2026, 11, 16, 8, 30, 0, tzinfo=timezone.utc))

        # Comma list: "0 9,17 * * *" (9 AM and 5 PM)
        ct_list = CronTrigger(cron_expression="0 9,17 * * *")
        nxt_list = ct_list.compute_next_run(datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc), tz_str="UTC")
        self.assertEqual(nxt_list, datetime(2026, 11, 13, 17, 0, 0, tzinfo=timezone.utc))

    def test_timezone_and_daylight_saving_transitions(self) -> None:
        """Verify DST transitions resolve local wall-clock time correctly."""
        # In New York (EDT -> EST): DST ends on Sunday Nov 1, 2026 at 2:00 AM (clocks move back to 1:00 AM)
        dt = DailyTrigger(time_str="09:00")
        tz_ny = "America/New_York"

        # Saturday Oct 31, 2026 (EDT, UTC-4) at 09:00 EDT = 13:00 UTC
        base_before = datetime(2026, 10, 31, 10, 0, 0, tzinfo=zoneinfo.ZoneInfo(tz_ny))
        nxt_dst = dt.compute_next_run(base_before, tz_str=tz_ny)
        # Next run is Nov 1, 2026 (EST, UTC-5) at 09:00 EST = 14:00 UTC
        self.assertEqual(nxt_dst.astimezone(zoneinfo.ZoneInfo(tz_ny)).hour, 9)
        self.assertEqual(nxt_dst.astimezone(zoneinfo.ZoneInfo(tz_ny)).minute, 0)
        self.assertEqual(nxt_dst.astimezone(timezone.utc).hour, 14)

    # -------------------------------------------------------------------------
    # 2. Schedule Repository & Atomic Claiming Tests
    # -------------------------------------------------------------------------

    async def test_atomic_claiming_across_workers(self) -> None:
        """Verify atomic claiming ensures no two workers claim the same schedule simultaneously."""
        # Create a test workflow
        async with self.session_factory() as session:
            wf_repo = WorkflowRepository(session)
            wf = await wf_repo.create_workflow(
                name="Atomic Claiming WF",
                user_id=123,
                steps=[{"name": "s1", "tool_name": "dummy_scheduler_tool", "arguments": {"message": "hi"}}],
            )
            wf_id = wf.id

        now = datetime.now(timezone.utc)
        # Schedule due immediately in the past
        schedule = await self.scheduler.schedule_workflow(
            workflow_id=wf_id,
            user_id=123,
            name="Claim Schedule",
            trigger_type="interval",
            trigger_config={"interval_seconds": 60},
        )

        # Force next_run_at to past so it is due
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.next_run_at = now - timedelta(minutes=5)
            await session.commit()

        # Worker 1 claims
        async with self.session_factory() as session1:
            repo1 = ScheduleRepository(session1)
            claimed_w1 = await repo1.claim_due_schedules(worker_id="worker_alpha", batch_size=5, lease_seconds=60)

        # Worker 2 attempts to claim concurrently
        async with self.session_factory() as session2:
            repo2 = ScheduleRepository(session2)
            claimed_w2 = await repo2.claim_due_schedules(worker_id="worker_beta", batch_size=5, lease_seconds=60)

        self.assertEqual(len(claimed_w1), 1)
        self.assertEqual(claimed_w1[0].id, schedule.id)
        self.assertEqual(claimed_w1[0].claim_token, "worker_alpha")

        # Worker 2 should get 0 schedules
        self.assertEqual(len(claimed_w2), 0)

    async def test_recover_stale_claims(self) -> None:
        """Verify stale claims past lease expiration are cleanly recovered."""
        async with self.session_factory() as session:
            wf_repo = WorkflowRepository(session)
            wf = await wf_repo.create_workflow(
                name="Stale Claim WF",
                user_id=456,
                steps=[{"name": "s1", "tool_name": "dummy_scheduler_tool", "arguments": {}}],
            )
            wf_id = wf.id

        schedule = await self.scheduler.schedule_workflow(
            workflow_id=wf_id,
            user_id=456,
            name="Stale Schedule",
            trigger_type="interval",
            trigger_config={"interval_seconds": 60},
        )

        now = datetime.now(timezone.utc)
        # Simulate worker crashed with lease expired 10 minutes ago
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.claim_token = "crashed_worker"
            sch.claim_expires_at = now - timedelta(minutes=10)
            sch.next_run_at = now - timedelta(minutes=10)
            await session.commit()

        # Run recovery
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            recovered_count = await sch_repo.recover_stale_claims()
            self.assertEqual(recovered_count, 1)

            # Schedule should now be claimable again by another worker
            claimed = await sch_repo.claim_due_schedules(worker_id="new_worker", batch_size=5)
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0].claim_token, "new_worker")

    # -------------------------------------------------------------------------
    # 3. Catch-Up Policy Tests
    # -------------------------------------------------------------------------

    async def test_catch_up_policy_run_once(self) -> None:
        """Verify run_once catch-up policy avoids burst storms after simulated downtime."""
        async with self.session_factory() as session:
            wf_repo = WorkflowRepository(session)
            wf = await wf_repo.create_workflow(
                name="Catch-up WF",
                user_id=789,
                steps=[{"name": "step1", "tool_name": "dummy_scheduler_tool", "arguments": {"message": "catchup"}}],
            )
            wf_id = wf.id

        schedule = await self.scheduler.schedule_workflow(
            workflow_id=wf_id,
            user_id=789,
            name="Catchup Run Once Schedule",
            trigger_type="interval",
            trigger_config={"interval_seconds": 60},
            catch_up_policy=CatchUpPolicy.RUN_ONCE.value,
        )

        now = datetime.now(timezone.utc)
        # Simulate 1 hour of downtime: schedule was due 60 minutes ago
        missed_time = now - timedelta(hours=1)
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            sch.next_run_at = missed_time
            await session.commit()

        # Trigger single poll cycle
        results = await self.scheduler._poll_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "success")

        # Verify next_run_at was scheduled strictly into the future, NOT backlogged
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(schedule.id)
            self.assertEqual(sch.total_runs_count, 1)
            self.assertGreater(_as_utc(sch.next_run_at), now)

    # -------------------------------------------------------------------------
    # 4. Lifecycle: Pause, Resume, Cancel Tests
    # -------------------------------------------------------------------------

    async def test_schedule_pause_resume_cancel(self) -> None:
        """Verify lifecycle state transitions for schedules."""
        async with self.session_factory() as session:
            wf_repo = WorkflowRepository(session)
            wf = await wf_repo.create_workflow(
                name="Lifecycle WF",
                user_id=999,
                steps=[{"name": "step1", "tool_name": "dummy_scheduler_tool", "arguments": {}}],
            )
            wf_id = wf.id

        sch = await self.scheduler.schedule_workflow(
            workflow_id=wf_id,
            user_id=999,
            name="Lifecycle Schedule",
            trigger_type="daily",
            trigger_config={"time": "09:00"},
        )
        self.assertEqual(sch.status, ScheduleStatus.ACTIVE.value)

        # Pause
        paused = await self.scheduler.pause_schedule(sch.id, user_id=999)
        self.assertTrue(paused)
        sch_paused = await self.scheduler.get_schedule(sch.id)
        self.assertEqual(sch_paused.status, ScheduleStatus.PAUSED.value)

        # Paused schedule is not claimed even if due
        async with self.session_factory() as session:
            repo = ScheduleRepository(session)
            sch_model = await repo.get_schedule(sch.id)
            sch_model.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await session.commit()
            claimed = await repo.claim_due_schedules(worker_id="test_worker")
            self.assertEqual(len(claimed), 0)

        # Resume
        resumed = await self.scheduler.resume_schedule(sch.id, user_id=999)
        self.assertTrue(resumed)
        sch_resumed = await self.scheduler.get_schedule(sch.id)
        self.assertEqual(sch_resumed.status, ScheduleStatus.ACTIVE.value)
        # Next run should have been recomputed into the future
        self.assertGreater(_as_utc(sch_resumed.next_run_at), datetime.now(timezone.utc))

        # Cancel
        cancelled = await self.scheduler.cancel_schedule(sch.id, user_id=999)
        self.assertTrue(cancelled)
        sch_cancelled = await self.scheduler.get_schedule(sch.id)
        self.assertEqual(sch_cancelled.status, ScheduleStatus.CANCELLED.value)

    # -------------------------------------------------------------------------
    # 5. End-to-End Scheduled Execution via WorkflowEngine
    # -------------------------------------------------------------------------

    async def test_end_to_end_scheduled_execution(self) -> None:
        """Verify scheduler dispatches execution through WorkflowEngine, executing tools and advancing checkpoints."""
        # 1. Create a workflow
        async with self.session_factory() as session:
            wf_repo = WorkflowRepository(session)
            wf = await wf_repo.create_workflow(
                name="E2E Scheduled Workflow",
                user_id=321,
                steps=[
                    {
                        "name": "auto_step",
                        "tool_name": "dummy_scheduler_tool",
                        "arguments": {"message": "scheduled_payload"},
                    }
                ],
            )
            wf_id = wf.id

        # 2. Schedule workflow with max_runs=1 (should complete after 1 execution)
        sch = await self.scheduler.schedule_workflow(
            workflow_id=wf_id,
            user_id=321,
            name="One-Shot Scheduled Run",
            trigger_type="interval",
            trigger_config={"interval_seconds": 60},
            max_runs=1,
        )

        # Make due immediately
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            repo = ScheduleRepository(session)
            sch_model = await repo.get_schedule(sch.id)
            sch_model.next_run_at = now - timedelta(seconds=1)
            await session.commit()

        # 3. Poll and dispatch
        results = await self.scheduler._poll_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "success")

        # Verify tool was invoked
        self.assertEqual(self.dummy_tool.call_count, 1)

        # Verify schedule advanced to completed
        sch_after = await self.scheduler.get_schedule(sch.id)
        self.assertEqual(sch_after.total_runs_count, 1)
        self.assertEqual(sch_after.status, ScheduleStatus.COMPLETED.value)
        self.assertIsNone(sch_after.next_run_at)
        self.assertIsNotNone(sch_after.last_run_at)

    async def test_workflow_control_tool_scheduler_actions(self) -> None:
        """Verify WorkflowControlTool actions: schedule_workflow, list_schedules, pause_schedule, resume_schedule, cancel_schedule."""
        from src.tools.builtin.workflow_tools import WorkflowControlTool

        wf_tool = WorkflowControlTool(
            workflow_engine=self.workflow_engine,
            workflow_scheduler=self.scheduler,
        )

        # Create workflow
        async with self.session_factory() as session:
            wf_repo = WorkflowRepository(session)
            wf = await wf_repo.create_workflow(
                name="Tool Scheduled WF",
                user_id=555,
                steps=[{"name": "step1", "tool_name": "dummy_scheduler_tool", "arguments": {}}],
            )
            wf_id = wf.id

        # 1. schedule_workflow
        res_sch = await wf_tool.execute(
            action="schedule_workflow",
            workflow_id=wf_id,
            user_id=555,
            name="My Daily Schedule",
            trigger_type="daily",
            trigger_config={"time": "12:00"},
            timezone="UTC",
        )
        self.assertEqual(res_sch["action"], "schedule_workflow")
        sch_id = res_sch["schedule_id"]
        self.assertIsNotNone(sch_id)

        # 2. list_schedules
        res_list = await wf_tool.execute(action="list_schedules", user_id=555)
        self.assertEqual(res_list["count"], 1)
        self.assertEqual(res_list["schedules"][0]["id"], sch_id)

        # 3. pause_schedule
        res_pause = await wf_tool.execute(action="pause_schedule", schedule_id=sch_id, user_id=555)
        self.assertTrue(res_pause["paused"])

        # 4. resume_schedule
        res_resume = await wf_tool.execute(action="resume_schedule", schedule_id=sch_id, user_id=555)
        self.assertTrue(res_resume["resumed"])

        # 5. cancel_schedule
        res_cancel = await wf_tool.execute(action="cancel_schedule", schedule_id=sch_id, user_id=555)
        self.assertTrue(res_cancel["cancelled"])


if __name__ == "__main__":
    unittest.main()
