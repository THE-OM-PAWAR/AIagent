"""Durable Outbox synchronization worker connecting PostgreSQL with Qdrant."""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.database.repositories.outbox_repo import OutboxRepository
from src.memory.embeddings.provider import EmbeddingProvider
from src.memory.storage.qdrant import QdrantStorage

logger = logging.getLogger(__name__)


class OutboxSynchronizer:
    """Consumes PostgreSQL transactional outbox entries and syncs them to Qdrant."""

    def __init__(
        self,
        qdrant_storage: QdrantStorage,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self.qdrant = qdrant_storage
        self.embedding = embedding_provider

    async def process_batch(
        self,
        session: AsyncSession,
        batch_size: int = 25,
        max_retries: int = 5,
    ) -> int:
        """Process a single batch of pending outbox tasks.

        Returns:
            Number of tasks successfully processed in this batch.
        """
        outbox_repo = OutboxRepository(session)
        tasks = await outbox_repo.fetch_pending_tasks(batch_size=batch_size, max_retries=max_retries)
        if not tasks:
            return 0

        success_count = 0
        for task in tasks:
            try:
                if task.operation in {"insert", "update"}:
                    memory = task.memory
                    if not memory or memory.status != "active":
                        # If deleted or inactive, remove vector
                        await self.qdrant.delete_vector(task.memory_id)
                    else:
                        vector = await self.embedding.generate_embedding(memory.content)
                        payload = {
                            "memory_id": memory.id,
                            "user_id": memory.user_id,
                            "category": memory.category,
                            "project_association": memory.project_association,
                            "status": memory.status,
                            "importance": memory.importance,
                            "confidence": memory.confidence,
                            "created_at": memory.created_at.isoformat(),
                            "content": memory.content,
                        }
                        await self.qdrant.upsert_vector(
                            memory_id=memory.id,
                            vector=vector,
                            payload=payload,
                        )
                elif task.operation == "delete":
                    await self.qdrant.delete_vector(task.memory_id)

                await outbox_repo.mark_completed(task.id)
                success_count += 1
            except Exception as exc:
                logger.error(
                    "Outbox sync failure for task_id=%s, memory_id=%s: %s",
                    task.id,
                    task.memory_id,
                    exc,
                    exc_info=True,
                )
                await outbox_repo.mark_failed(task.id, str(exc))

        await session.commit()
        return success_count

    async def reconcile(
        self,
        session: AsyncSession,
        max_batches: int = 20,
        batch_size: int = 50,
    ) -> int:
        """Continuously process outbox queue until fully synchronized or max batches reached."""
        total_synced = 0
        for _ in range(max_batches):
            count = await self.process_batch(session, batch_size=batch_size)
            total_synced += count
            if count == 0:
                break
        return total_synced

