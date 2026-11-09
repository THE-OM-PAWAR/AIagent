"""Repository managing directional relationships between memory records."""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models.relationship import MemoryRelationshipModel


class RelationshipRepository:
    """Manages memory-to-project, event-to-decision, and task-to-project links."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_relationship(
        self,
        user_id: int,
        source_memory_id: str,
        target_memory_id: str,
        relationship_type: str,
        metadata_extra: dict[str, Any] | None = None,
    ) -> MemoryRelationshipModel:
        """Create a new relationship link between two memories."""
        rel = MemoryRelationshipModel(
            user_id=user_id,
            source_memory_id=source_memory_id,
            target_memory_id=target_memory_id,
            relationship_type=relationship_type,
            metadata_extra=metadata_extra or {},
        )
        self.session.add(rel)
        await self.session.flush()
        return rel

    async def get_relationships_for_memory(
        self,
        user_id: int,
        memory_id: str,
    ) -> list[MemoryRelationshipModel]:
        """Fetch all inbound and outbound relationships for a memory."""
        stmt = select(MemoryRelationshipModel).where(
            MemoryRelationshipModel.user_id == user_id,
            (MemoryRelationshipModel.source_memory_id == memory_id)
            | (MemoryRelationshipModel.target_memory_id == memory_id),
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def delete_relationship(
        self,
        relationship_id: str,
        user_id: int,
    ) -> bool:
        """Delete a relationship by ID with user isolation."""
        stmt = delete(MemoryRelationshipModel).where(
            MemoryRelationshipModel.id == relationship_id,
            MemoryRelationshipModel.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.rowcount > 0

