"""Repository for persistent workflow state management, checkpoints, and recovery in PostgreSQL."""

from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models.workflow import WorkflowModel

logger = logging.getLogger(__name__)


class WorkflowRepository:
    """Async repository managing persistent workflows, step execution logs, and restart recovery."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_workflow(
        self,
        user_id: int,
        name: str,
        steps: list[dict[str, Any]],
        context_data: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> WorkflowModel:
        """Create and persist a new workflow in pending status."""
        wf = WorkflowModel(
            id=str(uuid.uuid4()),
            user_id=user_id,
            name=name,
            description=description,
            status="pending",
            current_step_index=0,
            total_steps=len(steps),
            steps=steps,
            context_data=context_data or {},
            execution_history=[],
            checkpoint_data={"step_index": 0, "status": "pending"},
        )
        self.session.add(wf)
        await self.session.commit()
        await self.session.refresh(wf)
        logger.info("Workflow created: id=%s, name='%s', steps=%d, user=%d", wf.id, name, len(steps), user_id)
        return wf

    async def get_workflow(self, workflow_id: str, user_id: int | None = None) -> WorkflowModel | None:
        """Retrieve workflow by ID, optionally validating user ownership."""
        stmt = select(WorkflowModel).where(WorkflowModel.id == workflow_id)
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

    async def update_status(
        self,
        workflow_id: str,
        status: str,
        error_message: str | None = None,
        completed: bool = False,
    ) -> bool:
        """Update workflow status and optional error message."""
        values: dict[str, Any] = {"status": status}
        if error_message is not None:
            values["error_message"] = error_message
        if completed:
            values["completed_at"] = datetime.now(timezone.utc)

        stmt = update(WorkflowModel).where(WorkflowModel.id == workflow_id).values(**values)
        res = await self.session.execute(stmt)
        await self.session.commit()
        return res.rowcount > 0

    async def save_step_checkpoint(
        self,
        workflow_id: str,
        step_index: int,
        context_data: dict[str, Any],
        history_record: dict[str, Any],
        next_status: str | None = None,
    ) -> bool:
        """Persist checkpoint after executing a workflow step."""
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
            values["status"] = next_status
            if next_status == "completed":
                values["completed_at"] = datetime.now(timezone.utc)

        stmt = update(WorkflowModel).where(WorkflowModel.id == workflow_id).values(**values)
        await self.session.execute(stmt)
        await self.session.commit()
        return True

    async def recover_interrupted_workflows(self) -> int:
        """Safely transition any abandoned 'running' workflows to 'paused' on restart."""
        stmt = (
            update(WorkflowModel)
            .where(WorkflowModel.status == "running")
            .values(
                status="paused",
                error_message="Workflow execution was interrupted by application restart; safely paused without duplicate execution.",
            )
        )
        res = await self.session.execute(stmt)
        await self.session.commit()
        if res.rowcount > 0:
            logger.info("Workflow recovery: safely paused %d interrupted running workflows.", res.rowcount)
        return res.rowcount

