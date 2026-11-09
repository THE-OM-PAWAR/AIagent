"""Multi-factor reranker combining semantic relevance, importance, confidence, recency, and project context."""

from datetime import datetime, timezone
import math

from src.memory.schemas.memory import MemoryRecord
from src.memory.schemas.retrieval import RetrievalResult


class MemoryReranker:
    """Reranks retrieval candidates using multi-factor contextual scoring."""

    def __init__(
        self,
        weight_relevance: float = 0.45,
        weight_importance: float = 0.20,
        weight_confidence: float = 0.15,
        weight_recency: float = 0.10,
        weight_project: float = 0.10,
        recency_half_life_days: float = 30.0,
    ) -> None:
        self.w_rel = weight_relevance
        self.w_imp = weight_importance
        self.w_conf = weight_confidence
        self.w_rec = weight_recency
        self.w_proj = weight_project
        self.decay_lambda = math.log(2) / max(1.0, recency_half_life_days)

    def rerank(
        self,
        fused_candidates: list[tuple[MemoryRecord, float]],
        target_project: str | None = None,
        top_k: int = 5,
        similarity_threshold: float = 0.0,
    ) -> list[RetrievalResult]:
        """Apply multi-factor scoring and return ordered RetrievalResult items."""
        if not fused_candidates:
            return []

        # Find max relevance score to normalize
        max_score = max(score for _, score in fused_candidates) or 1.0
        now = datetime.now(timezone.utc)

        results: list[RetrievalResult] = []
        for record, raw_score in fused_candidates:
            # 1. Normalized relevance score [0.0, 1.0]
            norm_rel = raw_score / max_score

            # 2. Importance and Confidence
            imp = max(0.0, min(1.0, record.importance))
            conf = max(0.0, min(1.0, record.confidence))

            # 3. Recency decay calculation
            created_at = record.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            delta_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
            recency_score = math.exp(-self.decay_lambda * delta_days)

            # 4. Project alignment bonus
            project_score = 0.0
            if target_project and record.project_association:
                if target_project.lower() == record.project_association.lower():
                    project_score = 1.0

            # Composite multi-factor score
            final_score = (
                (self.w_rel * norm_rel)
                + (self.w_imp * imp)
                + (self.w_conf * conf)
                + (self.w_rec * recency_score)
                + (self.w_proj * project_score)
            )

            if final_score < similarity_threshold:
                continue

            results.append(
                RetrievalResult(
                    memory_id=record.id,
                    category=record.category,
                    content=record.content,
                    importance=record.importance,
                    confidence=record.confidence,
                    relevance_score=round(final_score, 4),
                    project_association=record.project_association,
                    source_reference=record.source_reference,
                    created_at=record.created_at.isoformat(),
                    metadata=record.metadata_extra,
                )
            )

        # Sort descending by composite score and cap at top_k
        results.sort(key=lambda x: x.relevance_score, reverse=True)
        return results[:top_k]

