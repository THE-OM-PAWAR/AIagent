"""Complete hybrid RAG retrieval pipeline combining dense search, keyword search, RRF, and reranking."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.embeddings.provider import EmbeddingProvider
from src.memory.retrieval.dense import DenseRetriever
from src.memory.retrieval.fusion import ReciprocalRankFusion
from src.memory.retrieval.keyword import KeywordRetriever
from src.memory.retrieval.reranker import MemoryReranker
from src.memory.schemas.retrieval import RetrievalFilter, RetrievalResult
from src.memory.storage.qdrant import QdrantStorage

logger = logging.getLogger(__name__)


class RetrievalPipeline:
    """Coordinates hybrid vector and keyword retrieval with fusion and multi-factor reranking."""

    def __init__(
        self,
        qdrant_storage: QdrantStorage,
        embedding_provider: EmbeddingProvider,
        default_top_k: int = 5,
        default_similarity_threshold: float = 0.0,
    ) -> None:
        self.dense_retriever = DenseRetriever(qdrant_storage, embedding_provider)
        self.keyword_retriever = KeywordRetriever()
        self.fuser = ReciprocalRankFusion()
        self.reranker = MemoryReranker()
        self.default_top_k = default_top_k
        self.default_threshold = default_similarity_threshold

    async def retrieve(
        self,
        session: AsyncSession,
        query: str,
        retrieval_filter: RetrievalFilter,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
    ) -> list[RetrievalResult]:
        """Execute complete hybrid retrieval pipeline enforcing strict user-level isolation.

        Args:
            session: Active asynchronous PostgreSQL session.
            query: Query text prompt.
            retrieval_filter: Mandatory filter containing target user_id and optional category/project constraints.
            top_k: Optional maximum number of memories to return.
            similarity_threshold: Optional minimum composite score threshold.

        Returns:
            Ranked list of RetrievalResult objects.
        """
        clean_query = query.strip()
        if not clean_query:
            return []

        limit = max(10, (top_k or self.default_top_k) * 3)

        # Run dense and keyword retrievals in parallel
        dense_task = self.dense_retriever.retrieve(
            query=clean_query,
            retrieval_filter=retrieval_filter,
            limit=limit,
        )
        keyword_task = self.keyword_retriever.retrieve(
            session=session,
            query=clean_query,
            retrieval_filter=retrieval_filter,
            limit=limit,
        )

        dense_results, keyword_results = await asyncio.gather(
            dense_task,
            keyword_task,
            return_exceptions=False,
        )

        logger.debug(
            "Retrieval pipeline: found %d dense candidates, %d keyword candidates for user_id=%s",
            len(dense_results),
            len(keyword_results),
            retrieval_filter.user_id,
        )

        # Result fusion with Reciprocal Rank Fusion
        fused = self.fuser.fuse([dense_results, keyword_results])

        # Multi-factor reranking
        final_top_k = top_k or self.default_top_k
        threshold = self.default_threshold if similarity_threshold is None else similarity_threshold

        ranked_results = self.reranker.rerank(
            fused_candidates=fused,
            target_project=retrieval_filter.project_association,
            top_k=final_top_k,
            similarity_threshold=threshold,
        )

        return ranked_results

