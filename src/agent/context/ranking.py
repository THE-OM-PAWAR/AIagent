"""Multi-factor ranking and scoring for context items with decision preservation."""

from datetime import datetime, timezone
import logging
import math
import re
from typing import Any
from pydantic import BaseModel, Field

from src.memory.schemas.memory import FactType, MemoryCategory, MemoryRecord
from src.memory.schemas.retrieval import RetrievalResult

logger = logging.getLogger(__name__)

# Keywords indicating explicit constraints and decisions that must be protected
_DECISION_REGEX = re.compile(
    r"\b(decid(ed|ion)|chose|chosen|architect(ure)?|rule|guideline|selected|standard|design)\b",
    re.IGNORECASE,
)
_CONSTRAINT_REGEX = re.compile(
    r"\b(must|never|always|cannot|can't|do not|don't|prefer(ence|s)?|require(d|s)?|prohibit|constraint)\b",
    re.IGNORECASE,
)


class ScoredContextItem(BaseModel):
    """Context item augmented with multi-factor score, breakdown, and protection flag."""
    item_id: str
    category: str
    content: str
    relevance_score: float = 0.0
    importance: float = 0.5
    confidence: float = 1.0
    recency_factor: float = 1.0
    project_match: float = 0.0
    decision_bonus: float = 0.0
    final_score: float = 0.0
    is_protected: bool = False  # Protected items will not be truncated first during budget overflow
    is_decision: bool = False
    is_constraint: bool = False
    source_reference: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    original_item: Any = None


class ContextRanker:
    """Computes multi-factor scores and ranks candidate context items."""

    def __init__(
        self,
        relevance_weight: float = 0.35,
        importance_weight: float = 0.20,
        confidence_weight: float = 0.15,
        recency_weight: float = 0.15,
        project_weight: float = 0.10,
        decision_weight: float = 0.05,
        half_life_hours: float = 168.0,  # 7-day half life for recency decay
    ) -> None:
        self.w_rel = relevance_weight
        self.w_imp = importance_weight
        self.w_conf = confidence_weight
        self.w_rec = recency_weight
        self.w_proj = project_weight
        self.w_dec = decision_weight
        self.half_life_hours = max(1.0, half_life_hours)
        self.decay_constant = math.log(2.0) / self.half_life_hours

    def calculate_recency_factor(self, timestamp: datetime | str | None) -> float:
        """Compute exponential decay factor based on age in hours: exp(-lambda * delta_t)."""
        if not timestamp:
            return 0.7  # Default neutral recency

        dt = timestamp
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except Exception:
                return 0.7

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        age_hours = max(0.0, (now - dt).total_seconds() / 3600.0)
        return math.exp(-self.decay_constant * age_hours)

    def is_decision_or_constraint(self, text: str, category: str, fact_type: str | None = None) -> tuple[bool, bool]:
        """Detect whether text conveys an architectural decision or user constraint."""
        is_dec = bool(_DECISION_REGEX.search(text)) or (category == MemoryCategory.PROJECT.value)
        is_con = bool(_CONSTRAINT_REGEX.search(text)) or (fact_type == FactType.EXPLICIT_FACT.value and bool(_CONSTRAINT_REGEX.search(text)))
        return is_dec, is_con

    def score_retrieval_result(
        self,
        item: RetrievalResult,
        active_project: str | None = None,
    ) -> ScoredContextItem:
        """Score a RetrievalResult from the Phase 1 hybrid RAG retrieval pipeline."""
        content = item.content
        category = item.category.value if hasattr(item.category, "value") else str(item.category)
        fact_type = item.metadata.get("fact_type", "explicit_fact")

        # Project match
        proj_match = 0.0
        if active_project and item.project_association:
            if item.project_association.strip().lower() == active_project.strip().lower():
                proj_match = 1.0

        # Recency decay
        recency = self.calculate_recency_factor(item.created_at)

        # Decision & constraint detection
        is_dec, is_con = self.is_decision_or_constraint(content, category, fact_type)
        dec_bonus = 1.0 if (is_dec or is_con) else 0.0

        # Protection flag: high-confidence/high-importance constraints or explicit decisions
        is_protected = (
            (is_dec or is_con)
            and item.confidence >= 0.7
            and item.importance >= 0.6
        ) or (item.importance >= 0.9)

        # Multi-factor score
        rel_score = max(0.0, min(1.0, float(item.relevance_score)))
        imp_score = max(0.0, min(1.0, float(item.importance)))
        conf_score = max(0.0, min(1.0, float(item.confidence)))

        final_score = (
            self.w_rel * rel_score
            + self.w_imp * imp_score
            + self.w_conf * conf_score
            + self.w_rec * recency
            + self.w_proj * proj_match
            + self.w_dec * dec_bonus
        )

        return ScoredContextItem(
            item_id=item.memory_id,
            category=category,
            content=content,
            relevance_score=rel_score,
            importance=imp_score,
            confidence=conf_score,
            recency_factor=recency,
            project_match=proj_match,
            decision_bonus=dec_bonus,
            final_score=round(final_score, 4),
            is_protected=is_protected,
            is_decision=is_dec,
            is_constraint=is_con,
            source_reference=item.source_reference or {},
            metadata=dict(item.metadata),
            original_item=item,
        )

    def rank_items(
        self,
        items: list[RetrievalResult],
        active_project: str | None = None,
    ) -> list[ScoredContextItem]:
        """Score and sort items in descending order of final composite score."""
        scored = [self.score_retrieval_result(item, active_project) for item in items]
        scored.sort(key=lambda x: x.final_score, reverse=True)
        return scored

