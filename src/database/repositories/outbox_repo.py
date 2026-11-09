"""Repository managing the transactional outbox queue for Qdrant synchronization."""

from datetime import datetime, timezone
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database.models.base import utc_now
from src.database.models.outbox import MemoryOutboxModel

logger = logging.getLogger(__name__)


class OutboxRepository:
    """Manages transactional outbox tasks, retries, and status reconciliation."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fetch_pending_tasks(
        self,
        batch_size: int = 50,
        max_retries: int = 5,
    ) -> list[MemoryOutboxModel]:
        """Fetch pending or retryable outbox tasks eager-loading the associated memory."""
        stmt = (
            select(MemoryOutboxModel)
            .where(
                MemoryOutboxModel.status.in_(["pending", "failed"]),
                MemoryOutboxModel.retry_count < max_retries,
            )
            .options(selectinload(MemoryOutboxModel.memory))
            .order_by(MemoryOutboxModel.created_at.asc())
            .limit(batch_size)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def mark_completed(self, task_id: str) -> None:
        """Mark an outbox task as successfully synchronized."""
        stmt = (
            update(MemoryOutboxModel)
            .where(MemoryOutboxModel.id == task_id)
            .values(
                status="completed",
                updated_at=utc_now(),
                error_message=None,
            )
        )
        await self.session.execute(stmt)

    async def mark_failed(self, task_id: str, error_message: str) -> None:
        """Increment retry count and update failure error on outbox task."""
        stmt = (
            update(MemoryOutboxModel)
            .where(MemoryOutboxModel.id == task_id)
            .values(
                status="failed",
                retry_count=MemoryOutboxModel.retry_count + 1,
                error_message=error_message[:1000],
                updated_at=utc_now(),
            )
        )
        await self.session.execute(stmt)

