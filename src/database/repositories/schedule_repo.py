"""Repository for persistent workflow schedules, recurrence rules, and atomic claiming."""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database.models.workflow import CatchUpPolicy, ScheduleStatus, WorkflowScheduleModel

logger = logging.getLogger(__name__)


class ScheduleRepository:
    """Async repository managing workflow schedules, recurrence calculations, and atomic distributed worker claiming."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_schedule(
        self,
        workflow_id: str,
        user_id: int,
        name: str,
        trigger_type: str,
        trigger_config: dict[str, Any],
        timezone_str: str = "UTC",
        next_run_at: datetime | None = None,
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
        """Persist a new workflow schedule with its initial calculated next_run_at and execution policies."""
        sch_id = str(uuid.uuid4())
        schedule = WorkflowScheduleModel(
            id=sch_id,
            workflow_id=workflow_id,
            user_id=user_id,
            name=name,
            description=description,
            trigger_type=trigger_type,
            trigger_config=trigger_config or {},
            timezone=timezone_str,
            status=ScheduleStatus.ACTIVE.value,
            catch_up_policy=catch_up_policy,
            last_run_at=None,
            next_run_at=next_run_at,
            total_runs_count=0,
            max_runs=max_runs,
            allow_overlap=allow_overlap,
            max_concurrency=max_concurrency,
            consecutive_failures=0,
            max_consecutive_failures=max_consecutive_failures,
            run_timeout_seconds=run_timeout_seconds,
            api_call_budget=api_call_budget,
            execution_policy=execution_policy or {},
        )
        self.session.add(schedule)
        await self.session.commit()
        await self.session.refresh(schedule)

        logger.info(
            "Workflow schedule created: id=%s, workflow=%s, trigger=%s, next_run=%s, tz=%s, allow_overlap=%s",
            sch_id,
            workflow_id,
            trigger_type,
            next_run_at.isoformat() if next_run_at else "None",
            timezone_str,
            allow_overlap,
        )
        return schedule

    async def get_schedule(self, schedule_id: str, user_id: int | None = None) -> WorkflowScheduleModel | None:
        """Retrieve schedule by ID with workflow relationship loaded."""
        stmt = (
            select(WorkflowScheduleModel)
            .where(WorkflowScheduleModel.id == schedule_id)
            .options(selectinload(WorkflowScheduleModel.workflow))
        )
        if user_id is not None:
            stmt = stmt.where(WorkflowScheduleModel.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def list_schedules(
        self, user_id: int, status: str | None = None, limit: int = 50
    ) -> list[WorkflowScheduleModel]:
        """List schedules for a user."""
        stmt = select(WorkflowScheduleModel).where(WorkflowScheduleModel.user_id == user_id)
        if status:
            stmt = stmt.where(WorkflowScheduleModel.status == status)
        stmt = stmt.order_by(desc(WorkflowScheduleModel.created_at)).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(self, schedule_id: str, status: str, user_id: int | None = None) -> bool:
        """Update lifecycle status of a schedule (e.g. active, paused, cancelled, completed)."""
        values: dict[str, Any] = {"status": status}
        stmt = update(WorkflowScheduleModel).where(WorkflowScheduleModel.id == schedule_id)
        if user_id is not None:
            stmt = stmt.where(WorkflowScheduleModel.user_id == user_id)
        stmt = stmt.values(**values)
        res = await self.session.execute(stmt)
        await self.session.commit()
        return res.rowcount > 0

    async def claim_due_schedules(
        self,
        worker_id: str,
        batch_size: int = 10,
        lease_seconds: int = 60,
    ) -> list[WorkflowScheduleModel]:
        """Atomically claim due schedules using SELECT FOR UPDATE SKIP LOCKED or atomic updates."""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=lease_seconds)

        stmt = (
            select(WorkflowScheduleModel.id)
            .where(WorkflowScheduleModel.status == ScheduleStatus.ACTIVE.value)
            .where(WorkflowScheduleModel.next_run_at.is_not(None))
            .where(WorkflowScheduleModel.next_run_at <= now)
            .where(
                or_(
                    WorkflowScheduleModel.claim_expires_at.is_(None),
                    WorkflowScheduleModel.claim_expires_at < now,
                )
            )
            .order_by(WorkflowScheduleModel.next_run_at.asc())
            .limit(batch_size)
        )

        bind = self.session.bind
        if bind and bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        candidate_ids = list((await self.session.execute(stmt)).scalars().all())
        if not candidate_ids:
            return []

        # Claim selected candidates
        update_stmt = (
            update(WorkflowScheduleModel)
            .where(WorkflowScheduleModel.id.in_(candidate_ids))
            .where(
                or_(
                    WorkflowScheduleModel.claim_expires_at.is_(None),
                    WorkflowScheduleModel.claim_expires_at < now,
                    WorkflowScheduleModel.claim_token == worker_id,
                )
            )
            .values(claim_token=worker_id, claim_expires_at=expires_at)
        )
        await self.session.execute(update_stmt)
        await self.session.commit()

        # Retrieve claimed instances
        claimed_stmt = (
            select(WorkflowScheduleModel)
            .where(WorkflowScheduleModel.id.in_(candidate_ids))
            .where(WorkflowScheduleModel.claim_token == worker_id)
            .options(selectinload(WorkflowScheduleModel.workflow))
        )
        res = await self.session.execute(claimed_stmt)
        return list(res.scalars().all())

    async def advance_schedule_checkpoint(
        self,
        schedule_id: str,
        worker_id: str,
        next_run_at: datetime | None,
        is_completed: bool = False,
        is_success: bool = True,
    ) -> bool:
        """Update last_run_at, next_run_at, increment total_runs_count, track failures, and release claim."""
        now = datetime.now(timezone.utc)
        sch = await self.get_schedule(schedule_id)
        current_fails = getattr(sch, "consecutive_failures", 0) if sch else 0
        max_fails = getattr(sch, "max_consecutive_failures", None) if sch else None

        new_fails = 0 if is_success else (current_fails + 1)
        auto_paused = False

        values: dict[str, Any] = {
            "last_run_at": now,
            "next_run_at": next_run_at,
            "total_runs_count": WorkflowScheduleModel.total_runs_count + 1,
            "consecutive_failures": new_fails,
            "claim_token": None,
            "claim_expires_at": None,
        }
        if is_completed or next_run_at is None:
            values["status"] = ScheduleStatus.COMPLETED.value
            values["next_run_at"] = None
        elif not is_success and max_fails is not None and new_fails >= max_fails:
            values["status"] = ScheduleStatus.PAUSED.value
            auto_paused = True
            logger.warning(
                "Schedule %s auto-paused after reaching %d consecutive failure(s) (threshold: %d).",
                schedule_id,
                new_fails,
                max_fails,
            )

        stmt = (
            update(WorkflowScheduleModel)
            .where(WorkflowScheduleModel.id == schedule_id)
            .where(WorkflowScheduleModel.claim_token == worker_id)
            .values(**values)
        )
        res = await self.session.execute(stmt)
        await self.session.commit()
        return res.rowcount > 0

    async def release_claim(self, schedule_id: str, worker_id: str) -> bool:
        """Release worker claim without advancing schedule."""
        stmt = (
            update(WorkflowScheduleModel)
            .where(WorkflowScheduleModel.id == schedule_id)
            .where(WorkflowScheduleModel.claim_token == worker_id)
            .values(claim_token=None, claim_expires_at=None)
        )
        res = await self.session.execute(stmt)
        await self.session.commit()
        return res.rowcount > 0

    async def recover_stale_claims(self) -> int:
        """Release claims where claim_expires_at has expired."""
        now = datetime.now(timezone.utc)
        stmt = (
            update(WorkflowScheduleModel)
            .where(WorkflowScheduleModel.claim_expires_at.is_not(None))
            .where(WorkflowScheduleModel.claim_expires_at < now)
            .values(claim_token=None, claim_expires_at=None)
        )
        res = await self.session.execute(stmt)
        await self.session.commit()
        if res.rowcount > 0:
            logger.info("Recovered %d stale workflow schedule claims.", res.rowcount)
        return res.rowcount

