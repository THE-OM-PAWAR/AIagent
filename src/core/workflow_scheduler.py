"""Production-grade PostgreSQL-backed Workflow Scheduler and Trigger Dispatcher."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.triggers import (
    BaseTrigger,
    OneTimeTrigger,
    TriggerValidationError,
    _as_utc,
    create_trigger,
)
from src.core.workflow_engine import WorkflowEngine
from src.database.models.workflow import CatchUpPolicy, ScheduleStatus, WorkflowScheduleModel, WorkflowState
from src.database.repositories.schedule_repo import ScheduleRepository
from src.database.repositories.workflow_repo import WorkflowRepository

logger = logging.getLogger(__name__)


class WorkflowScheduler:
    """PostgreSQL-backed distributed scheduler with atomic claiming, catch-up policies, and pure-Python recurrence."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        workflow_engine: WorkflowEngine,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 10,
        lease_seconds: int = 60,
        worker_id: str | None = None,
        notification_handler: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.workflow_engine = workflow_engine
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:12]}"
        self.notification_handler = notification_handler

        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def _notify_user(self, user_id: int, message: str, run_id: str | None = None) -> None:
        """Send notification via registered handler, avoiding duplicate alerts for the same run."""
        if run_id:
            async with self.session_factory() as session:
                wf_repo = WorkflowRepository(session)
                run = await wf_repo.get_run(run_id)
                if run and run.notification_sent:
                    return
                await wf_repo.mark_notification_sent(run_id)

        if self.notification_handler:
            try:
                if asyncio.iscoroutinefunction(self.notification_handler):
                    await self.notification_handler(user_id, message)
                else:
                    self.notification_handler(user_id, message)
            except Exception as exc:
                logger.warning("Failed sending scheduled workflow notification to user %s: %s", user_id, exc)

    @property
    def is_running(self) -> bool:
        """Check if scheduler loop is running."""
        return self._running and self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the background scheduler polling loop."""
        if self._running:
            logger.warning("WorkflowScheduler is already running.")
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name=f"scheduler_{self.worker_id}")
        logger.info("WorkflowScheduler started with worker_id=%s (poll_interval=%.1fs)", self.worker_id, self.poll_interval_seconds)

    async def stop(self) -> None:
        """Stop the background scheduler polling loop cleanly."""
        if not self._running:
            return

        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("WorkflowScheduler stopped (worker_id=%s)", self.worker_id)

    async def _poll_loop(self) -> None:
        """Internal asynchronous polling loop."""
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("WorkflowScheduler poll loop error: %s", exc, exc_info=True)

            try:
                await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                break

    async def _poll_once(self) -> list[dict[str, Any]]:
        """Perform a single polling cycle: recover stale claims, claim due schedules, and dispatch executions."""
        now_utc = datetime.now(timezone.utc)
        dispatched_results: list[dict[str, Any]] = []

        # 1. Recover stale claims from crashed/timed-out workers
        async with self.session_factory() as session:
            repo = ScheduleRepository(session)
            await repo.recover_stale_claims()

        # 2. Atomically claim due schedules
        claimed_schedules: list[WorkflowScheduleModel] = []
        async with self.session_factory() as session:
            repo = ScheduleRepository(session)
            claimed_schedules = await repo.claim_due_schedules(
                worker_id=self.worker_id,
                batch_size=self.batch_size,
                lease_seconds=self.lease_seconds,
            )

        if not claimed_schedules:
            return []

        logger.info(
            "Worker %s claimed %d due schedule(s) for execution.",
            self.worker_id,
            len(claimed_schedules),
        )

        # 3. Dispatch each claimed schedule
        for schedule in claimed_schedules:
            try:
                # Instantiate trigger and compute next occurrence
                try:
                    trigger = create_trigger(
                        schedule.trigger_type,
                        schedule.trigger_config or {},
                        tz_str=schedule.timezone,
                    )
                except Exception as trigger_err:
                    logger.error(
                        "Schedule %s has invalid trigger configuration: %s",
                        schedule.id,
                        trigger_err,
                    )
                    # Mark schedule completed/failed to prevent endless retries
                    async with self.session_factory() as session:
                        repo = ScheduleRepository(session)
                        await repo.advance_schedule_checkpoint(
                            schedule_id=schedule.id,
                            worker_id=self.worker_id,
                            next_run_at=None,
                            is_completed=True,
                        )
                    continue

                # Determine base time for next run based on catch-up policy
                policy = schedule.catch_up_policy or CatchUpPolicy.RUN_ONCE.value
                if policy == CatchUpPolicy.RUN_ALL.value:
                    base_time = _as_utc(schedule.next_run_at) if schedule.next_run_at else now_utc
                else:
                    # RUN_ONCE and SKIP_ALL both calculate the next occurrence strictly after now
                    base_time = now_utc

                next_run_at = trigger.compute_next_run(base_time, tz_str=schedule.timezone)
                is_one_time = isinstance(trigger, OneTimeTrigger)
                runs_after_this = schedule.total_runs_count + 1
                is_completed = (
                    is_one_time
                    or next_run_at is None
                    or (schedule.max_runs is not None and runs_after_this >= schedule.max_runs)
                )

                # Concurrency and overlapping execution check
                allow_overlap = getattr(schedule, "allow_overlap", False)
                max_concurrency = getattr(schedule, "max_concurrency", 1) or 1
                if not allow_overlap:
                    async with self.session_factory() as session:
                        wf_repo = WorkflowRepository(session)
                        active_count = await wf_repo.count_active_runs(
                            workflow_id=schedule.workflow_id,
                            schedule_id=schedule.id,
                        )
                    if active_count >= max_concurrency:
                        logger.info(
                            "Schedule %s (workflow %s) has %d active run(s) and allow_overlap=False. Deferring occurrence to prevent concurrency conflict.",
                            schedule.id,
                            schedule.workflow_id,
                            active_count,
                        )
                        async with self.session_factory() as session:
                            repo = ScheduleRepository(session)
                            await repo.advance_schedule_checkpoint(
                                schedule_id=schedule.id,
                                worker_id=self.worker_id,
                                next_run_at=next_run_at,
                                is_completed=is_completed,
                                is_success=True,
                            )
                        continue

                # Prepare idempotent execution context and dispatch to WorkflowEngine
                occurrence_time_str = schedule.next_run_at.isoformat() if schedule.next_run_at else now_utc.isoformat()
                idempotency_key = f"schedule:{schedule.id}:{occurrence_time_str}"
                run_context = {
                    "schedule_id": schedule.id,
                    "schedule_name": schedule.name,
                    "trigger_type": schedule.trigger_type,
                    "scheduled_time": occurrence_time_str,
                    "api_call_budget": getattr(schedule, "api_call_budget", None),
                    "run_timeout_seconds": getattr(schedule, "run_timeout_seconds", None),
                }

                logger.info(
                    "Dispatching scheduled workflow %s for user %s (schedule_id=%s, trigger=%s)",
                    schedule.workflow_id,
                    schedule.user_id,
                    schedule.id,
                    schedule.trigger_type,
                )

                run_result: dict[str, Any] | None = None
                exec_error: str | None = None
                try:
                    run_result = await self.workflow_engine.execute_workflow(
                        workflow_id=schedule.workflow_id,
                        user_id=schedule.user_id,
                        trigger_type=f"scheduler:{schedule.trigger_type}",
                        initial_context_override=run_context,
                        idempotency_key=idempotency_key,
                        schedule_id=schedule.id,
                    )
                    st = run_result.get("status", "completed")
                    status_label = "success" if st in (WorkflowState.COMPLETED.value, "success") else st
                    dispatched_results.append({
                        "schedule_id": schedule.id,
                        "workflow_id": schedule.workflow_id,
                        "status": status_label,
                        "run_result": run_result,
                    })
                except Exception as exec_err:
                    exec_error = str(exec_err)
                    logger.error(
                        "Workflow execution failed for schedule %s (workflow=%s): %s",
                        schedule.id,
                        schedule.workflow_id,
                        exec_err,
                        exc_info=True,
                    )
                    dispatched_results.append({
                        "schedule_id": schedule.id,
                        "workflow_id": schedule.workflow_id,
                        "status": "error",
                        "error": exec_error,
                    })

                is_success = bool(
                    run_result
                    and run_result.get("status") in (
                        WorkflowState.COMPLETED.value,
                        WorkflowState.WAITING_FOR_APPROVAL.value,
                    )
                )

                # Advance schedule checkpoint and release claim
                async with self.session_factory() as session:
                    repo = ScheduleRepository(session)
                    await repo.advance_schedule_checkpoint(
                        schedule_id=schedule.id,
                        worker_id=self.worker_id,
                        next_run_at=next_run_at,
                        is_completed=is_completed,
                        is_success=is_success,
                    )

                # Notifications & Alerts
                run_id = run_result.get("run_id") if run_result else None
                policy = getattr(schedule, "execution_policy", {}) or {}

                if run_result and run_result.get("status") == WorkflowState.WAITING_FOR_APPROVAL.value:
                    appr_id = run_result.get("approval_id")
                    step_nm = run_result.get("step_name")
                    tool_nm = run_result.get("tool_name")
                    notify_text = (
                        f"🛡️ <b>Workflow Action Approval Required</b>\n\n"
                        f"• Workflow: <b>{schedule.name}</b>\n"
                        f"• Step: <code>{step_nm}</code> ({tool_nm})\n"
                        f"• Approval ID: <code>{appr_id}</code>\n\n"
                        f"💡 Approve: <code>/workflows approve {appr_id}</code>\n"
                        f"💡 Reject: <code>/workflows reject {appr_id}</code>"
                    )
                    await self._notify_user(schedule.user_id, notify_text, run_id=run_id)

                elif is_success and policy.get("notify_on_success", True):
                    nxt_str = next_run_at.strftime('%Y-%m-%d %H:%M:%S UTC') if next_run_at else "None"
                    notify_text = (
                        f"✅ <b>Scheduled Workflow Completed</b>\n\n"
                        f"• Workflow: <b>{schedule.name}</b>\n"
                        f"• Schedule ID: <code>{schedule.id}</code>\n"
                        f"• Run ID: <code>{run_id}</code>\n"
                        f"• Next run: <code>{nxt_str}</code>"
                    )
                    await self._notify_user(schedule.user_id, notify_text, run_id=run_id)

                elif not is_success and policy.get("notify_on_failure", True):
                    err_msg = (run_result.get("error") if run_result else exec_error) or "Execution error."
                    notify_text = (
                        f"⚠️ <b>Scheduled Workflow Execution Failed</b>\n\n"
                        f"• Workflow: <b>{schedule.name}</b>\n"
                        f"• Schedule ID: <code>{schedule.id}</code>\n"
                        f"• Error: <code>{err_msg}</code>\n"
                        f"• Consecutive Failures: {schedule.consecutive_failures + 1}"
                    )
                    await self._notify_user(schedule.user_id, notify_text, run_id=run_id)

            except Exception as sch_err:
                logger.error(
                    "Unexpected error processing schedule %s: %s",
                    schedule.id,
                    sch_err,
                    exc_info=True,
                )
                # Ensure worker claim is released so it does not stay deadlocked
                async with self.session_factory() as session:
                    repo = ScheduleRepository(session)
                    await repo.release_claim(schedule.id, self.worker_id)

        return dispatched_results

    # -------------------------------------------------------------------------
    # Schedule Management APIs
    # -------------------------------------------------------------------------

    async def schedule_workflow(
        self,
        workflow_id: str,
        user_id: int,
        name: str,
        trigger_type: str,
        trigger_config: dict[str, Any],
        timezone_str: str = "UTC",
        description: str | None = None,
        catch_up_policy: str = "run_once",
        max_runs: int | None = None,
        allow_overlap: bool = False,
        max_concurrency: int = 1,
        max_consecutive_failures: int | None = None,
        run_timeout_seconds: int | None = None,
        api_call_budget: int | None = None,
        execution_policy: dict[str, Any] | None = None,
    ) -> WorkflowScheduleModel:
        """Create, validate, calculate initial run timestamp, and persist a new workflow schedule."""
        # 1. Validate trigger and configuration
        trigger = create_trigger(trigger_type, trigger_config, tz_str=timezone_str)

        # 2. Compute initial next_run_at
        now_utc = datetime.now(timezone.utc)
        if isinstance(trigger, OneTimeTrigger):
            if trigger.is_immediate:
                initial_next_run = now_utc
            else:
                initial_next_run = trigger.scheduled_at or now_utc
        else:
            initial_next_run = trigger.compute_next_run(now_utc, tz_str=timezone_str)

        # Validate catch-up policy
        if catch_up_policy not in (CatchUpPolicy.RUN_ONCE.value, CatchUpPolicy.SKIP_ALL.value, CatchUpPolicy.RUN_ALL.value):
            catch_up_policy = CatchUpPolicy.RUN_ONCE.value

        async with self.session_factory() as session:
            repo = ScheduleRepository(session)
            schedule = await repo.create_schedule(
                workflow_id=workflow_id,
                user_id=user_id,
                name=name,
                trigger_type=trigger_type,
                trigger_config=trigger_config,
                timezone_str=timezone_str,
                next_run_at=initial_next_run,
                description=description,
                catch_up_policy=catch_up_policy,
                max_runs=max_runs,
                allow_overlap=allow_overlap,
                max_concurrency=max_concurrency,
                max_consecutive_failures=max_consecutive_failures,
                run_timeout_seconds=run_timeout_seconds,
                api_call_budget=api_call_budget,
                execution_policy=execution_policy or {},
            )
            return schedule

    async def get_schedule(self, schedule_id: str, user_id: int | None = None) -> WorkflowScheduleModel | None:
        """Retrieve schedule by ID."""
        async with self.session_factory() as session:
            repo = ScheduleRepository(session)
            return await repo.get_schedule(schedule_id, user_id)

    async def list_schedules(
        self, user_id: int, status: str | None = None, limit: int = 50
    ) -> list[WorkflowScheduleModel]:
        """List schedules for a given user."""
        async with self.session_factory() as session:
            repo = ScheduleRepository(session)
            return await repo.list_schedules(user_id, status=status, limit=limit)

    async def pause_schedule(self, schedule_id: str, user_id: int | None = None) -> bool:
        """Pause an active schedule."""
        async with self.session_factory() as session:
            repo = ScheduleRepository(session)
            sch = await repo.get_schedule(schedule_id, user_id)
            if not sch:
                return False
            if sch.status != ScheduleStatus.ACTIVE.value:
                return False
            return await repo.update_status(schedule_id, ScheduleStatus.PAUSED.value, user_id)

    async def resume_schedule(self, schedule_id: str, user_id: int | None = None) -> bool:
        """Resume a paused schedule, recalculating next_run_at if it is in the past."""
        async with self.session_factory() as session:
            repo = ScheduleRepository(session)
            sch = await repo.get_schedule(schedule_id, user_id)
            if not sch:
                return False
            if sch.status != ScheduleStatus.PAUSED.value:
                return False

            now_utc = datetime.now(timezone.utc)
            # Recompute next_run_at if missing or already in the past
            next_run = sch.next_run_at
            if next_run is None or _as_utc(next_run) <= now_utc:
                try:
                    trigger = create_trigger(sch.trigger_type, sch.trigger_config or {}, tz_str=sch.timezone)
                    next_run = trigger.compute_next_run(now_utc, tz_str=sch.timezone)
                except Exception as exc:
                    logger.warning("Could not recompute next_run_at for schedule %s: %s", schedule_id, exc)

            stmt = (
                ScheduleRepository(session)
                .session
            )
            # Update status and recalculated next_run_at
            from sqlalchemy import update
            update_stmt = (
                update(WorkflowScheduleModel)
                .where(WorkflowScheduleModel.id == schedule_id)
                .values(status=ScheduleStatus.ACTIVE.value, next_run_at=next_run)
            )
            if user_id is not None:
                update_stmt = update_stmt.where(WorkflowScheduleModel.user_id == user_id)
            res = await session.execute(update_stmt)
            await session.commit()
            return res.rowcount > 0

    async def cancel_schedule(self, schedule_id: str, user_id: int | None = None) -> bool:
        """Cancel a schedule permanently."""
        async with self.session_factory() as session:
            repo = ScheduleRepository(session)
            return await repo.update_status(schedule_id, ScheduleStatus.CANCELLED.value, user_id)
