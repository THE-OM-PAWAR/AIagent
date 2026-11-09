"""Repository managing PostgreSQL operations for memories and full-text keyword retrieval."""

from datetime import datetime, timezone
import logging
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models.base import utc_now
from src.database.models.memory import MemoryModel
from src.database.models.outbox import MemoryOutboxModel

logger = logging.getLogger(__name__)


class MemoryRepository:
    """Manages transactional CRUD, lifecycle state transitions, and keyword search."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_memory(
        self,
        user_id: int,
        category: str,
        content: str,
        source_reference: dict[str, Any] | None = None,
        project_association: str | None = None,
        importance: float = 0.5,
        confidence: float = 1.0,
        fact_type: str = "explicit_fact",
        status: str = "active",
        expires_at: datetime | None = None,
        metadata_extra: dict[str, Any] | None = None,
    ) -> MemoryModel:
        """Create a new memory and atomically enqueue an outbox synchronization task."""
        memory = MemoryModel(
            user_id=user_id,
            category=category,
            content=content.strip(),
            source_reference=source_reference or {},
            project_association=project_association,
            importance=max(0.0, min(1.0, float(importance))),
            confidence=max(0.0, min(1.0, float(confidence))),
            fact_type=fact_type,
            status=status,
            expires_at=expires_at,
            metadata_extra=metadata_extra or {},
        )
        self.session.add(memory)
        await self.session.flush()

        # Enqueue transactional outbox task for vector storage synchronization
        outbox_entry = MemoryOutboxModel(
            memory_id=memory.id,
            user_id=user_id,
            operation="insert",
            status="pending",
        )
        self.session.add(outbox_entry)
        await self.session.flush()

        logger.debug("Created memory id=%s and outbox task for user_id=%s", memory.id, user_id)
        return memory

    async def get_by_id(self, memory_id: str, user_id: int) -> MemoryModel | None:
        """Retrieve a specific memory enforcing strict user isolation."""
        stmt = select(MemoryModel).where(
            MemoryModel.id == memory_id,
            MemoryModel.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def update_memory(
        self,
        memory_id: str,
        user_id: int,
        content: str | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        project_association: str | None = None,
        metadata_extra: dict[str, Any] | None = None,
    ) -> MemoryModel | None:
        """Update memory fields and enqueue an outbox update task."""
        memory = await self.get_by_id(memory_id, user_id)
        if not memory:
            return None

        content_changed = False
        if content is not None and content.strip() != memory.content:
            memory.content = content.strip()
            content_changed = True
        if importance is not None:
            memory.importance = max(0.0, min(1.0, float(importance)))
        if confidence is not None:
            memory.confidence = max(0.0, min(1.0, float(confidence)))
        if project_association is not None:
            memory.project_association = project_association
        if metadata_extra is not None:
            memory.metadata_extra = metadata_extra

        memory.updated_at = utc_now()

        # Enqueue update outbox task
        outbox_entry = MemoryOutboxModel(
            memory_id=memory.id,
            user_id=user_id,
            operation="update",
            status="pending",
        )
        self.session.add(outbox_entry)
        await self.session.flush()
        return memory

    async def set_status(
        self,
        memory_id: str,
        user_id: int,
        status: str,
    ) -> bool:
        """Set memory lifecycle status (e.g. archived, invalidated, expired)."""
        memory = await self.get_by_id(memory_id, user_id)
        if not memory:
            return False

        memory.status = status
        memory.updated_at = utc_now()

        # If deactivated or invalidated, enqueue outbox task to remove from or update Qdrant
        op = "delete" if status in {"invalidated", "expired", "archived"} else "update"
        outbox_entry = MemoryOutboxModel(
            memory_id=memory.id,
            user_id=user_id,
            operation=op,
            status="pending",
        )
        self.session.add(outbox_entry)
        await self.session.flush()
        return True

    async def list_active(
        self,
        user_id: int,
        category: str | None = None,
        project_association: str | None = None,
        limit: int = 100,
    ) -> list[MemoryModel]:
        """List active memories for a user with optional filters."""
        stmt = select(MemoryModel).where(
            MemoryModel.user_id == user_id,
            MemoryModel.status == "active",
        )
        if category:
            stmt = stmt.where(MemoryModel.category == category)
        if project_association:
            stmt = stmt.where(MemoryModel.project_association == project_association)

        stmt = stmt.order_by(MemoryModel.importance.desc(), MemoryModel.created_at.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def search_keyword(
        self,
        user_id: int,
        query: str,
        category: str | None = None,
        project_association: str | None = None,
        limit: int = 20,
    ) -> list[tuple[MemoryModel, float]]:
        """Perform keyword search with relevance ranking across active user memories."""
        clean_query = query.strip()
        if not clean_query:
            return []

        stmt = select(MemoryModel).where(
            MemoryModel.user_id == user_id,
            MemoryModel.status == "active",
        )
        if category:
            stmt = stmt.where(MemoryModel.category == category)
        if project_association:
            stmt = stmt.where(MemoryModel.project_association == project_association)

        # Check database dialect: if PostgreSQL, use native ts_rank and websearch_to_tsquery
        bind = self.session.get_bind()
        dialect_name = bind.dialect.name if bind else "postgresql"

        if dialect_name == "postgresql":
            ts_query = func.websearch_to_tsquery("english", clean_query)
            ts_vector = func.to_tsvector("english", MemoryModel.content)
            rank = func.ts_rank_cd(ts_vector, ts_query)

            stmt = stmt.where(ts_vector.op("@@")(ts_query))
            stmt = stmt.add_columns(rank.label("rank_score"))
            stmt = stmt.order_by(rank.desc()).limit(limit)

            result = await self.session.execute(stmt)
            matches = []
            for row in result.all():
                mem = row[0]
                score = float(row[1]) if len(row) > 1 else 1.0
                matches.append((mem, score))
            return matches
        else:
            # Universal fallback (ILIKE) for in-memory SQLite test environments
            terms = clean_query.split()
            for term in terms[:5]:
                stmt = stmt.where(MemoryModel.content.ilike(f"%{term}%"))

            stmt = stmt.limit(limit)
            result = await self.session.execute(stmt)
            memories = result.scalars().all()
            # Calculate simple term frequency score
            ranked_matches: list[tuple[MemoryModel, float]] = []
            for mem in memories:
                content_lower = mem.content.lower()
                matches_count = sum(1 for t in terms if t.lower() in content_lower)
                score = matches_count / max(1, len(terms))
                ranked_matches.append((mem, score))

            ranked_matches.sort(key=lambda x: x[1], reverse=True)
            return ranked_matches

    async def expire_old_memories(self, user_id: int | None = None) -> int:
        """Mark all expired temporary/working memories as 'expired'."""
        now = utc_now()
        stmt = select(MemoryModel).where(
            MemoryModel.status == "active",
            MemoryModel.expires_at.is_not(None),
            MemoryModel.expires_at <= now,
        )
        if user_id is not None:
            stmt = stmt.where(MemoryModel.user_id == user_id)

        result = await self.session.execute(stmt)
        expired_list = result.scalars().all()

        count = 0
        for memory in expired_list:
            memory.status = "expired"
            memory.updated_at = now
            outbox_entry = MemoryOutboxModel(
                memory_id=memory.id,
                user_id=memory.user_id,
                operation="delete",
                status="pending",
            )
            self.session.add(outbox_entry)
            count += 1

        if count > 0:
            await self.session.flush()
            logger.info("Marked %d temporary memories as expired", count)
        return count

