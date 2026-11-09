"""Reciprocal Rank Fusion (RRF) and candidate deduplication."""

from collections import defaultdict

from src.memory.schemas.memory import MemoryRecord
from src.memory.schemas.retrieval import RetrievalCandidate


class ReciprocalRankFusion:
    """Combines ranking lists from disparate search modalities into a unified fused score."""

    def __init__(self, k: int = 60) -> None:
        self.k = k

    def fuse(
        self,
        candidate_lists: list[list[RetrievalCandidate]],
    ) -> list[tuple[MemoryRecord, float]]:
        """Compute RRF score for each unique candidate across multiple retrieval result sets.

        Args:
            candidate_lists: List of candidate rankings from dense and keyword searches.

        Returns:
            List of (MemoryRecord, rrf_score) ordered by descending RRF score.
        """
        rrf_scores: dict[str, float] = defaultdict(float)
        record_map: dict[str, MemoryRecord] = {}

        for candidates in candidate_lists:
            for item in candidates:
                record_map[item.memory_id] = item.record
                # RRF formula: 1.0 / (k + rank)
                rrf_scores[item.memory_id] += 1.0 / (self.k + item.rank)

        # Sort descending by fused score
        ranked = sorted(
            [(record_map[mid], score) for mid, score in rrf_scores.items()],
            key=lambda x: x[1],
            reverse=True,
        )
        return ranked

