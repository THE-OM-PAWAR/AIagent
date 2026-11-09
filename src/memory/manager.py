"""High-level unified Memory Manager orchestrating storage, RAG retrieval, ingestion, and sync."""

from datetime import datetime
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models.memory import MemoryModel
from src.database.models.relationship import MemoryRelationshipModel
from src.database.repositories.memory_repo import MemoryRepository
from src.database.session import get_async_session
from src.memory.embeddings.provider import EmbeddingProvider
from src.memory.ingestion.pipeline import IngestionPipeline, IngestionResult
from src.memory.relationships.manager import RelationshipManager
from src.memory.retrieval.pipeline import RetrievalPipeline
from src.memory.schemas.memory import (
    MemoryCandidate,
    MemoryCategory,
    MemoryRecord,
    MemoryStatus,
    MemoryUpdate,
    SourceReference,
)
from src.memory.schemas.retrieval import RetrievalFilter, RetrievalResult
from src.memory.storage.outbox import OutboxSynchronizer
from src.memory.storage.qdrant import QdrantStorage

logger = logging.getLogger(__name__)


class MemoryManager:
    """Production-grade memory manager unifying PostgreSQL, Qdrant vector storage, and RAG retrieval."""

    def __init__(
        self,
        database_url: str,
        qdrant_storage: QdrantStorage,
        embedding_provider: EmbeddingProvider,
        gemini_client: Any = None,
        default_top_k: int = 5,
        default_similarity_threshold: float = 0.0,
    ) -> None:
        self.database_url = database_url
        self.qdrant = qdrant_storage
        self.embedding = embedding_provider
        self.ingestion = IngestionPipeline()
        self.retrieval = RetrievalPipeline(
            qdrant_storage=self.qdrant,
            embedding_provider=self.embedding,
            default_top_k=default_top_k,
            default_similarity_threshold=default_similarity_threshold,
        )
        self.outbox_sync = OutboxSynchronizer(
            qdrant_storage=self.qdrant,
            embedding_provider=self.embedding,
        )
        self.relationships = RelationshipManager()

    async def initialize(self) -> None:
        """Initialize underlying vector collections and embedding clients."""
        logger.info("Initializing MemoryManager subsystems...")
        self.embedding.initialize()
        await self.qdrant.initialize()
        logger.info("MemoryManager initialized successfully.")

    # --------------------------------------------------------------------------
    # 1. Ingestion & Extraction
    # --------------------------------------------------------------------------
    async def remember_turn(
        self,
        user_id: int,
        user_text: str,
        conversation_id: str | None = None,
        message_id: str | int | None = None,
    ) -> IngestionResult:
        """Extract and persist memories from a user conversation turn."""
        async with get_async_session(self.database_url) as session:
            result = await self.ingestion.ingest_conversation_turn(
                session=session,
                user_id=user_id,
                user_text=user_text,
                conversation_id=conversation_id,
                message_id=message_id,
            )
            # Synchronize pending outbox items directly within process flow
            await self.outbox_sync.process_batch(session)
            return result

    async def remember(
        self,
        user_id: int,
        candidate: MemoryCandidate,
        source_reference: SourceReference | dict[str, Any] | None = None,
    ) -> MemoryRecord | None:
        """Directly remember a validated candidate memory."""
        async with get_async_session(self.database_url) as session:
            model = await self.ingestion.ingest_candidate(
                session=session,
                user_id=user_id,
                candidate=candidate,
                source_reference=source_reference,
            )
            if model:
                await self.outbox_sync.process_batch(session)
                return MemoryRecord.model_validate(model)
            return None

    # --------------------------------------------------------------------------
    # 2. RAG Retrieval Pipeline
    # --------------------------------------------------------------------------
    async def retrieve(
        self,
        user_id: int,
        query: str,
        categories: list[MemoryCategory] | None = None,
        project_association: str | None = None,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
    ) -> list[RetrievalResult]:
        """Execute hybrid RAG retrieval pipeline strictly isolated to the specified user."""
        retrieval_filter = RetrievalFilter(
            user_id=user_id,
            categories=categories,
            project_association=project_association,
            status=MemoryStatus.ACTIVE,
        )
        async with get_async_session(self.database_url) as session:
            results = await self.retrieval.retrieve(
                session=session,
                query=query,
                retrieval_filter=retrieval_filter,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
            )
            return results

    # --------------------------------------------------------------------------
    # 3. CRUD & Invalidation
    # --------------------------------------------------------------------------
    async def get_memory(self, user_id: int, memory_id: str) -> MemoryRecord | None:
        """Fetch a specific memory record by ID enforcing user isolation."""
        async with get_async_session(self.database_url) as session:
            repo = MemoryRepository(session)
            model = await repo.get_by_id(memory_id=memory_id, user_id=user_id)
            if model:
                return MemoryRecord.model_validate(model)
            return None

    async def update_memory(
        self,
        user_id: int,
        memory_id: str,
        update_data: MemoryUpdate,
    ) -> MemoryRecord | None:
        """Update memory fields and trigger vector synchronization."""
        async with get_async_session(self.database_url) as session:
            repo = MemoryRepository(session)
            model = await repo.update_memory(
                memory_id=memory_id,
                user_id=user_id,
                content=update_data.content,
                importance=update_data.importance,
                confidence=update_data.confidence,
                project_association=update_data.project_association,
                metadata_extra=update_data.metadata_extra,
            )
            if model and update_data.status:
                await repo.set_status(memory_id, user_id, update_data.status.value)
            if model:
                await self.outbox_sync.process_batch(session)
                return MemoryRecord.model_validate(model)
            return None

    async def invalidate_memory(self, user_id: int, memory_id: str) -> bool:
        """Invalidate an established memory and remove it from vector search."""
        async with get_async_session(self.database_url) as session:
            repo = MemoryRepository(session)
            success = await repo.set_status(memory_id=memory_id, user_id=user_id, status="invalidated")
            if success:
                await self.outbox_sync.process_batch(session)
            return success

    async def archive_memory(self, user_id: int, memory_id: str) -> bool:
        """Archive an established memory."""
        async with get_async_session(self.database_url) as session:
            repo = MemoryRepository(session)
            success = await repo.set_status(memory_id=memory_id, user_id=user_id, status="archived")
            if success:
                await self.outbox_sync.process_batch(session)
            return success

    # --------------------------------------------------------------------------
    # 4. Outbox Sync & Expiration Maintenance
    # --------------------------------------------------------------------------
    async def sync_outbox(self, batch_size: int = 50) -> int:
        """Drain pending outbox tasks."""
        async with get_async_session(self.database_url) as session:
            return await self.outbox_sync.process_batch(session, batch_size=batch_size)

    async def expire_memories(self, user_id: int | None = None) -> int:
        """Expire outdated working/temporary memories."""
        async with get_async_session(self.database_url) as session:
            repo = MemoryRepository(session)
            count = await repo.expire_old_memories(user_id=user_id)
            if count > 0:
                await self.outbox_sync.process_batch(session)
            return count

    # --------------------------------------------------------------------------
    # 5. Relationships
    # --------------------------------------------------------------------------
    async def link_memories(
        self,
        user_id: int,
        source_memory_id: str,
        target_memory_id: str,
        relationship_type: str,
        metadata_extra: dict[str, Any] | None = None,
    ) -> MemoryRelationshipModel:
        """Link two memories with a directional relationship."""
        async with get_async_session(self.database_url) as session:
            return await self.relationships.link_memories(
                session=session,
                user_id=user_id,
                source_memory_id=source_memory_id,
                target_memory_id=target_memory_id,
                relationship_type=relationship_type,
                metadata_extra=metadata_extra,
            )

    async def get_relationships(
        self,
        user_id: int,
        memory_id: str,
    ) -> list[MemoryRelationshipModel]:
        """Fetch all relationships associated with a memory."""
        async with get_async_session(self.database_url) as session:
            return await self.relationships.get_relationships(
                session=session,
                user_id=user_id,
                memory_id=memory_id,
            )

    async def close(self) -> None:
        """Close Qdrant and storage connections."""
        await self.qdrant.close()

