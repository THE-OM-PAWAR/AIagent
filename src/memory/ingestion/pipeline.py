"""Memory ingestion pipeline coordinating extraction, validation, deduplication, and storage."""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models.conflict import MemoryConflictModel
from src.database.models.memory import MemoryModel
from src.database.repositories.memory_repo import MemoryRepository
from src.memory.ingestion.conflict import ConflictDetector
from src.memory.ingestion.deduplication import DuplicateDetector
from src.memory.ingestion.extractor import MemoryExtractor
from src.memory.schemas.memory import MemoryCandidate, SourceReference

logger = logging.getLogger(__name__)


class IngestionResult:
    """Result summary of an ingestion attempt."""

    def __init__(
        self,
        created: list[MemoryModel] | None = None,
        duplicates: list[MemoryModel] | None = None,
        conflicts: list[str] | None = None,
    ) -> None:
        self.created = created or []
        self.duplicates = duplicates or []
        self.conflicts = conflicts or []


class IngestionPipeline:
    """Coordinates memory extraction, deduplication, conflict management, and PostgreSQL persistence."""

    def __init__(self, extractor: MemoryExtractor | None = None) -> None:
        self.extractor = extractor or MemoryExtractor()
        self.duplicate_detector = DuplicateDetector()
        self.conflict_detector = ConflictDetector()

    async def ingest_candidate(
        self,
        session: AsyncSession,
        user_id: int,
        candidate: MemoryCandidate,
        source_reference: SourceReference | dict[str, Any] | None = None,
    ) -> MemoryModel | None:
        """Validate, deduplicate, conflict-check, and persist a single candidate memory."""
        repo = MemoryRepository(session)
        source_dict = (
            source_reference.model_dump()
            if isinstance(source_reference, SourceReference)
            else (source_reference or {})
        )

        # 1. Fetch user's existing active memories in this category
        existing_memories = await repo.list_active(
            user_id=user_id,
            category=candidate.category.value,
        )

        # 2. Check for exact or near duplicate
        duplicate = self.duplicate_detector.find_duplicate(candidate, existing_memories)
        if duplicate:
            logger.info(
                "Ingestion: skipped duplicate memory for user_id=%s (existing_id=%s)",
                user_id,
                duplicate.id,
            )
            return duplicate

        # 3. Check for conflict with established knowledge
        conflict = self.conflict_detector.detect_conflict(candidate, existing_memories)
        if conflict.has_conflict:
            assert conflict.existing_memory is not None
            if not conflict.should_overwrite:
                # Preserve established memory without overwriting. Log conflict for review.
                logger.warning(
                    "Ingestion: conflict detected for user_id=%s: %s. Preserving established memory id=%s",
                    user_id,
                    conflict.reason,
                    conflict.existing_memory.id,
                )
                conflict_entry = MemoryConflictModel(
                    user_id=user_id,
                    existing_memory_id=conflict.existing_memory.id,
                    conflicting_content=candidate.content,
                    reason=conflict.reason or "Unresolved contradiction",
                    status="unresolved",
                    source_reference=source_dict,
                )
                session.add(conflict_entry)
                await session.flush()
                return None
            else:
                # Explicit high-confidence fact overwrites lower-confidence inference
                logger.info(
                    "Ingestion: resolving conflict by superseding old memory id=%s with new explicit fact",
                    conflict.existing_memory.id,
                )
                await repo.set_status(conflict.existing_memory.id, user_id, "invalidated")

        # 4. Persist approved memory to PostgreSQL (transactionally creates outbox entry)
        memory = await repo.create_memory(
            user_id=user_id,
            category=candidate.category.value,
            content=candidate.content,
            source_reference=source_dict,
            project_association=candidate.project_association,
            importance=candidate.importance,
            confidence=candidate.confidence,
            fact_type=candidate.fact_type.value,
            expires_at=candidate.expires_at,
            metadata_extra=candidate.metadata_extra,
        )
        return memory

    async def ingest_conversation_turn(
        self,
        session: AsyncSession,
        user_id: int,
        user_text: str,
        conversation_id: str | None = None,
        message_id: str | int | None = None,
    ) -> IngestionResult:
        """Extract candidate memories from user message and ingest all valid items."""
        candidates = await self.extractor.extract_candidates(user_text, user_id=user_id)
        if not candidates:
            return IngestionResult()

        source_ref = SourceReference(
            conversation_id=conversation_id,
            message_id=message_id,
            excerpt=user_text[:200],
        )

        result = IngestionResult()
        for cand in candidates:
            created_mem = await self.ingest_candidate(
                session=session,
                user_id=user_id,
                candidate=cand,
                source_reference=source_ref,
            )
            if created_mem:
                result.created.append(created_mem)

        return result

