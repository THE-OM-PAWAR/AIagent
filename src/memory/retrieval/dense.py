"""Dense semantic retrieval component utilizing Qdrant vector storage."""

import logging
from typing import Any

from src.memory.embeddings.provider import EmbeddingProvider
from src.memory.schemas.memory import MemoryCategory, MemoryRecord
from src.memory.schemas.retrieval import RetrievalCandidate, RetrievalFilter
from src.memory.storage.qdrant import QdrantStorage

logger = logging.getLogger(__name__)


class DenseRetriever:
    """Performs dense vector similarity search with user-isolated metadata filtering."""

    def __init__(
        self,
        qdrant_storage: QdrantStorage,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self.qdrant = qdrant_storage
        self.embedding = embedding_provider

    async def retrieve(
        self,
        query: str,
        retrieval_filter: RetrievalFilter,
        limit: int = 20,
    ) -> list[RetrievalCandidate]:
        """Perform dense vector search in Qdrant."""
        clean_query = query.strip()
        if not clean_query:
            return []

        # Generate query vector
        query_vector = await self.embedding.generate_embedding(clean_query)

        cat_str = retrieval_filter.categories[0].value if retrieval_filter.categories and len(retrieval_filter.categories) == 1 else None

        scored_points = await self.qdrant.search_dense(
            user_id=retrieval_filter.user_id,
            query_vector=query_vector,
            category=cat_str,
            project_association=retrieval_filter.project_association,
            limit=limit,
            score_threshold=0.0,
        )

        candidates: list[RetrievalCandidate] = []
        for rank, (mem_id, score, payload) in enumerate(scored_points, start=1):
            try:
                # Convert payload back into MemoryRecord
                rec = MemoryRecord(
                    id=mem_id,
                    user_id=payload.get("user_id", retrieval_filter.user_id),
                    category=MemoryCategory(payload.get("category", "semantic")),
                    content=payload.get("content", ""),
                    source_reference=payload.get("source_reference", {}),
                    project_association=payload.get("project_association"),
                    importance=float(payload.get("importance", 0.5)),
                    confidence=float(payload.get("confidence", 1.0)),
                    fact_type=payload.get("fact_type", "explicit_fact"),
                    status=payload.get("status", "active"),
                    created_at=payload.get("created_at"),
                    updated_at=payload.get("created_at"),
                )
                candidates.append(
                    RetrievalCandidate(
                        memory_id=mem_id,
                        record=rec,
                        source="dense",
                        score=score,
                        rank=rank,
                    )
                )
            except Exception as exc:
                logger.debug("Skipping unparseable Qdrant point %s: %s", mem_id, exc)

        return candidates

