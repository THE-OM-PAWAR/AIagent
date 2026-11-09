"""Memory relationships manager coordinating entity connections without graph databases."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models.relationship import MemoryRelationshipModel
from src.database.repositories.relationship_repo import RelationshipRepository


class RelationshipManager:
    """Coordinates memory-to-project, event-to-decision, and task-to-project links."""

    async def link_memories(
        self,
        session: AsyncSession,
        user_id: int,
        source_memory_id: str,
        target_memory_id: str,
        relationship_type: str,
        metadata_extra: dict[str, Any] | None = None,
    ) -> MemoryRelationshipModel:
        """Create a directional link between two memories enforcing user isolation."""
        repo = RelationshipRepository(session)
        return await repo.add_relationship(
            user_id=user_id,
            source_memory_id=source_memory_id,
            target_memory_id=target_memory_id,
            relationship_type=relationship_type,
            metadata_extra=metadata_extra,
        )

    async def get_relationships(
        self,
        session: AsyncSession,
        user_id: int,
        memory_id: str,
    ) -> list[MemoryRelationshipModel]:
        """Fetch all relationships connected to a memory."""
        repo = RelationshipRepository(session)
        return await repo.get_relationships_for_memory(user_id=user_id, memory_id=memory_id)

    async def remove_relationship(
        self,
        session: AsyncSession,
        user_id: int,
        relationship_id: str,
    ) -> bool:
        """Remove a relationship link."""
        repo = RelationshipRepository(session)
        return await repo.delete_relationship(relationship_id=relationship_id, user_id=user_id)

