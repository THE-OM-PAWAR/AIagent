"""Repository for persistent workflow state management, steps, runs, checkpoints, leases, and recovery in PostgreSQL."""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import and_, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database.models.workflow import (
    WorkflowModel,
    WorkflowRunModel,
    WorkflowState,
    WorkflowStepModel,
    validate_workflow_state_transition,
)

logger = logging.getLogger(__name__)


class WorkflowConcurrencyError(Exception):
    """Raised when an operation conflicts with an active execution lease held by another worker."""
    pass


class WorkflowRepository:
    """Async repository managing persistent workflows, steps, runs, execution logs, and restart recovery."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_workflow(
        self,
        user_id: int,
        name: str,
        steps: list[dict[str, Any]],
        context_data: dict[str, Any] | None = None,
        description: str | None = None,
        is_one_time: bool = True,
        initial_status: str = "pending",
    ) -> WorkflowModel:
        """Create and persist a new workflow and its normalized step definitions."""
        wf_id = str(uuid.uuid4())
        normalized_steps_json: list[dict[str, Any]] = []
        step_models: list[WorkflowStepModel] = []

        for idx, s in enumerate(steps):
            step_name = s.get("name") or f"step_{idx + 1}"
            tool_name = s.get("tool_name", "")
            args = s.get("arguments") if "arguments" in s else s.get("input_template", {})
            deps = s.get("dependencies", [])
            cond = s.get("condition")
            retries = s.get("max_retries", 1)
            timeout = s.get("timeout_seconds", 60)

            step_def_dict = {
                "name": step_name,
                "tool_name": tool_name,
                "arguments": args,
                "dependencies": deps,
                "condition": cond,
                "max_retries": retries,
                "timeout_seconds": timeout,
            }
            normalized_steps_json.append(step_def_dict)

            step_models.append(
                WorkflowStepModel(
                    id=str(uuid.uuid4()),
                    workflow_id=wf_id,
                    step_order=idx,
                    name=step_name,
                    tool_name=tool_name,
                    input_template=args or {},
                    dependencies=deps or [],
                    condition=cond,
                    max_retries=retries,
                    timeout_seconds=timeout,
                    status="pending",
                )
            )

        wf = WorkflowModel(
            id=wf_id,
            user_id=user_id,
            name=name,
            description=description,
            is_one_time=is_one_time,
            status=initial_status,
            current_step_index=0,
            total_steps=len(steps),
            steps=normalized_steps_json,
            context_data=context_data or {},
            execution_history=[],
            checkpoint_data={
                "step_index": 0,
                "status": initial_status,
                "state_transitions": [
                    {
                        "event": "workflow_created",
                        "status": initial_status,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ],
            },
        )
        self.session.add(wf)
        for sm in step_models:
            self.session.add(sm)

        await self.session.commit()
        await self.session.refresh(wf)
        logger.info(
            "Workflow created: id=%s, name='%s', steps=%d, user=%d, status=%s",
            wf.id,
            name,
            len(steps),
            user_id,
            initial_status,
        )
        return wf

    async def get_workflow(self, workflow_id: str, user_id: int | None = None) -> WorkflowModel | None:
        """Retrieve workflow by ID, optionally validating user ownership."""
        stmt = (
            select(WorkflowModel)
            .where(WorkflowModel.id == workflow_id)
            .options(
                selectinload(WorkflowModel.step_definitions),
                selectinload(WorkflowModel.runs),
                selectinload(WorkflowModel.approvals),
            )
        )
        if user_id is not None:
            stmt = stmt.where(WorkflowModel.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def list_workflows(
        self, user_id: int, status: str | None = None, limit: int = 20
    ) -> list[WorkflowModel]:
        """List workflows for a user with optional status filter."""
        stmt = select(WorkflowModel).where(WorkflowModel.user_id == user_id)
        if status:
            stmt = stmt.where(WorkflowModel.status == status)
        stmt = stmt.order_by(desc(WorkflowModel.created_at)).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create_run(
        self,
        workflow_id: str,
        user_id: int,
        trigger_type: str = "manual",
        initial_context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        schedule_id: str | None = None,
    ) -> WorkflowRunModel:
        """Create a new isolated execution run instance for a workflow with idempotency and schedule support."""
        if idempotency_key:
            stmt = (
                select(WorkflowRunModel)
                .where(WorkflowRunModel.workflow_id == workflow_id)
                .where(WorkflowRunModel.idempotency_key == idempotency_key)
            )
            existing = (await self.session.execute(stmt)).scalars().first()
            if existing:
                logger.info("Idempotent run matched existing run_id=%s for key=%s", existing.id, idempotency_key)
                return existing

        run = WorkflowRunModel(
            id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            user_id=user_id,
            schedule_id=schedule_id,
            status=WorkflowState.PENDING.value,
            trigger_type=trigger_type,
            context_data=dict(initial_context or {}),
            current_step_index=0,
            step_runs=[],
            idempotency_key=idempotency_key,
            started_at=datetime.now(timezone.utc),
        )
        self.session.add(run)
        await self.session.commit()
        await self.session.refresh(run)
        logger.info("Workflow run created: id=%s for workflow=%s (schedule_id=%s)", run.id, workflow_id, schedule_id)
        return run

    async def get_run(self, run_id: str, user_id: int | None = None) -> WorkflowRunModel | None:
        """Retrieve a specific workflow run record with approvals."""
        stmt = (
            select(WorkflowRunModel)
            .where(WorkflowRunModel.id == run_id)
            .options(selectinload(WorkflowRunModel.approvals))
        )
        if user_id is not None:
            stmt = stmt.where(WorkflowRunModel.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def count_active_runs(
        self,
        workflow_id: str | None = None,
        schedule_id: str | None = None,
    ) -> int:
        """Count runs currently executing or waiting for approval."""
        stmt = select(func.count(WorkflowRunModel.id)).where(
            WorkflowRunModel.status.in_([
                WorkflowState.RUNNING.value,
                WorkflowState.WAITING_FOR_APPROVAL.value,
            ])
        )
        if workflow_id:
            stmt = stmt.where(WorkflowRunModel.workflow_id == workflow_id)
        if schedule_id:
            stmt = stmt.where(WorkflowRunModel.schedule_id == schedule_id)
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

    async def list_runs_for_schedule(
        self,
        schedule_id: str,
        limit: int = 50,
    ) -> list[WorkflowRunModel]:
        """List execution runs generated by a specific schedule."""
        stmt = (
            select(WorkflowRunModel)
            .where(WorkflowRunModel.schedule_id == schedule_id)
            .order_by(desc(WorkflowRunModel.created_at))
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def mark_notification_sent(self, run_id: str) -> bool:
        """Mark notification_sent=True on a run record to prevent duplicate alerts."""
        stmt = (
            update(WorkflowRunModel)
            .where(WorkflowRunModel.id == run_id)
            .values(notification_sent=True)
        )
        res = await self.session.execute(stmt)
        await self.session.commit()
        return res.rowcount > 0

    async def list_runs(
        self, workflow_id: str, user_id: int | None = None, limit: int = 20
    ) -> list[WorkflowRunModel]:
        """List execution runs for a workflow."""
        stmt = select(WorkflowRunModel).where(WorkflowRunModel.workflow_id == workflow_id)
        if user_id is not None:
            stmt = stmt.where(WorkflowRunModel.user_id == user_id)
        stmt = stmt.order_by(desc(WorkflowRunModel.created_at)).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # -------------------------------------------------------------------------
    # Lease & Concurrency Management
    # -------------------------------------------------------------------------

    async def acquire_execution_lease(
        self,
        run_id: str,
        worker_token: str,
        lease_seconds: int = 60,
    ) -> bool:
        """Atomically acquire or re-acquire exclusive execution lease on a run."""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=lease_seconds)

        stmt = (
            update(WorkflowRunModel)
            .where(WorkflowRunModel.id == run_id)
            .where(
                or_(
                    WorkflowRunModel.lease_token.is_(None),
                    WorkflowRunModel.lease_expires_at < now,
                    WorkflowRunModel.lease_token == worker_token,
                )
            )
            .values(lease_token=worker_token, lease_expires_at=expires_at)
        )
        res = await self.session.execute(stmt)
        await self.session.commit()
        acquired = res.rowcount > 0
        if not acquired:
            logger.warning("Worker %s failed acquiring lease for run %s (currently locked)", worker_token, run_id)
        return acquired

    async def renew_execution_lease(
        self,
        run_id: str,
        worker_token: str,
        lease_seconds: int = 60,
    ) -> bool:
        """Renew active execution lease held by worker_token."""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=lease_seconds)

        stmt = (
            update(WorkflowRunModel)
            .where(WorkflowRunModel.id == run_id)
            .where(WorkflowRunModel.lease_token == worker_token)
            .values(lease_expires_at=expires_at)
        )
        res = await self.session.execute(stmt)
        await self.session.commit()
        return res.rowcount > 0

    async def release_execution_lease(
        self,
        run_id: str,
        worker_token: str,
    ) -> bool:
        """Release worker lease upon step completion, pause, or terminal state."""
        stmt = (
            update(WorkflowRunModel)
            .where(WorkflowRunModel.id == run_id)
            .where(WorkflowRunModel.lease_token == worker_token)
            .values(lease_token=None, lease_expires_at=None)
        )
        res = await self.session.execute(stmt)
        await self.session.commit()
        return res.rowcount > 0

    # -------------------------------------------------------------------------
    # State Transitions & Checkpoints
    # -------------------------------------------------------------------------

    async def update_status(
        self,
        workflow_id: str,
        status: str,
        error_message: str | None = None,
        completed: bool = False,
    ) -> bool:
        """Enforce valid state transition and persist workflow status."""
        wf = await self.get_workflow(workflow_id)
        if not wf:
            return False

        # Enforce state transition rules
        validate_workflow_state_transition(wf.status, status)

        transition_record = {
            "event": "state_transition",
            "from_status": wf.status,
            "to_status": status,
            "error_message": error_message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        cp = dict(wf.checkpoint_data or {})
        transitions = list(cp.get("state_transitions", []))
        transitions.append(transition_record)
        cp["state_transitions"] = transitions
        cp["status"] = status

        values: dict[str, Any] = {
            "status": status,
            "checkpoint_data": cp,
        }
        if error_message is not None:
            values["error_message"] = error_message
        if completed or status in (WorkflowState.COMPLETED.value, WorkflowState.CANCELLED.value):
            values["completed_at"] = datetime.now(timezone.utc)

        stmt = update(WorkflowModel).where(WorkflowModel.id == workflow_id).values(**values)
        res = await self.session.execute(stmt)
        await self.session.commit()
        return res.rowcount > 0

    async def update_run_status(
        self,
        run_id: str,
        status: str,
        error_message: str | None = None,
        completed: bool = False,
    ) -> bool:
        """Enforce valid transition and update status of a workflow run and parent workflow."""
        run = await self.get_run(run_id)
        if not run:
            return False

        validate_workflow_state_transition(run.status, status)

        values: dict[str, Any] = {"status": status}
        if error_message is not None:
            values["error_message"] = error_message
        if completed or status in (WorkflowState.COMPLETED.value, WorkflowState.CANCELLED.value):
            values["completed_at"] = datetime.now(timezone.utc)

        stmt = update(WorkflowRunModel).where(WorkflowRunModel.id == run_id).values(**values)
        await self.session.execute(stmt)

        # Sync parent workflow status safely (for one-time workflows only)
        wf = await self.get_workflow(run.workflow_id)
        if wf and not wf.is_one_time:
            # Recurring workflow definition stays pending/active across individual run cycles
            pass
        else:
            try:
                await self.update_status(
                    workflow_id=run.workflow_id,
                    status=status,
                    error_message=error_message,
                    completed=completed,
                )
            except ValueError as exc:
                logger.warning("Parent workflow sync transition skipped: %s", exc)

        await self.session.commit()
        return True

    async def save_step_checkpoint(
        self,
        workflow_id: str,
        step_index: int,
        context_data: dict[str, Any],
        history_record: dict[str, Any],
        next_status: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        """Persist transactional checkpoint after executing a workflow step for both workflow and run."""
        wf = await self.get_workflow(workflow_id)
        if not wf:
            return False

        updated_history = list(wf.execution_history or [])
        updated_history.append(history_record)

        values: dict[str, Any] = {
            "current_step_index": step_index,
            "context_data": context_data,
            "execution_history": updated_history,
            "checkpoint_data": {
                "step_index": step_index,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
        if next_status:
            if not wf.is_one_time:
                # For recurring workflows, individual runs complete, fail, or pause independently.
                # The workflow definition itself stays pending so future occurrences can execute.
                values["status"] = WorkflowState.PENDING.value
                if next_status in (WorkflowState.COMPLETED.value, WorkflowState.FAILED.value):
                    values["current_step_index"] = 0
            else:
                validate_workflow_state_transition(wf.status, next_status)
                values["status"] = next_status
                if next_status in (WorkflowState.COMPLETED.value, WorkflowState.CANCELLED.value):
                    values["completed_at"] = datetime.now(timezone.utc)

        stmt = update(WorkflowModel).where(WorkflowModel.id == workflow_id).values(**values)
        await self.session.execute(stmt)

        # Update WorkflowStepModel if matching step found
        step_name = history_record.get("step_name")
        if step_name:
            step_stmt = (
                select(WorkflowStepModel)
                .where(WorkflowStepModel.workflow_id == workflow_id)
                .where(WorkflowStepModel.name == step_name)
            )
            step_res = await self.session.execute(step_stmt)
            step_model = step_res.scalars().first()
            if step_model:
                step_model.status = history_record.get("status", "completed")
                step_model.output_data = (
                    context_data.get(step_name)
                    if isinstance(context_data.get(step_name), dict)
                    else {"result": context_data.get(step_name)}
                )
                step_model.error_message = history_record.get("error")

        # Update run if run_id provided
        if run_id:
            run = await self.get_run(run_id)
            if run:
                run_step_runs = list(run.step_runs or [])
                run_step_runs.append(history_record)
                run_values: dict[str, Any] = {
                    "current_step_index": step_index,
                    "context_data": context_data,
                    "step_runs": run_step_runs,
                }
                if next_status:
                    run_values["status"] = next_status
                    if next_status in (WorkflowState.COMPLETED.value, WorkflowState.CANCELLED.value):
                        run_values["completed_at"] = datetime.now(timezone.utc)
                await self.session.execute(
                    update(WorkflowRunModel).where(WorkflowRunModel.id == run_id).values(**run_values)
                )

        await self.session.commit()
        return True

    async def recover_interrupted_workflows(self) -> int:
        """Safely transition any abandoned 'running' workflows and runs to 'paused' on restart."""
        now = datetime.now(timezone.utc)
        stmt = (
            update(WorkflowModel)
            .where(WorkflowModel.status == WorkflowState.RUNNING.value)
            .values(
                status=WorkflowState.PAUSED.value,
                error_message="Workflow execution was interrupted by application restart; safely paused without duplicate execution.",
            )
        )
        res = await self.session.execute(stmt)

        # Release all worker leases and pause running runs
        await self.session.execute(
            update(WorkflowRunModel)
            .where(WorkflowRunModel.status == WorkflowState.RUNNING.value)
            .values(
                status=WorkflowState.PAUSED.value,
                lease_token=None,
                lease_expires_at=None,
                error_message="Run was interrupted by application restart; safely paused.",
            )
        )
        await self.session.commit()
        if res.rowcount > 0:
            logger.info("Workflow recovery: safely paused %d interrupted running workflows.", res.rowcount)
        return res.rowcount
