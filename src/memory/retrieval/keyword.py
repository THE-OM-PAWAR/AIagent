"""Keyword retrieval component executing PostgreSQL full-text search."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from src.database.repositories.memory_repo import MemoryRepository
from src.memory.schemas.memory import MemoryRecord
from src.memory.schemas.retrieval import RetrievalCandidate, RetrievalFilter

logger = logging.getLogger(__name__)


class KeywordRetriever:
    """Performs full-text keyword retrieval from PostgreSQL."""

    async def retrieve(
        self,
        session: AsyncSession,
        query: str,
        retrieval_filter: RetrievalFilter,
        limit: int = 20,
    ) -> list[RetrievalCandidate]:
        """Perform keyword search in PostgreSQL matching active memories."""
        clean_query = query.strip()
        if not clean_query:
            return []

        repo = MemoryRepository(session)
        cat_str = (
            retrieval_filter.categories[0].value
            if retrieval_filter.categories and len(retrieval_filter.categories) == 1
            else None
        )

        matches = await repo.search_keyword(
            user_id=retrieval_filter.user_id,
            query=clean_query,
            category=cat_str,
            project_association=retrieval_filter.project_association,
            limit=limit,
        )

        candidates: list[RetrievalCandidate] = []
        for rank, (mem_model, score) in enumerate(matches, start=1):
            rec = MemoryRecord.model_validate(mem_model)
            candidates.append(
                RetrievalCandidate(
                    memory_id=rec.id,
                    record=rec,
                    source="keyword",
                    score=score,
                    rank=rank,
                )
            )

        return candidates

