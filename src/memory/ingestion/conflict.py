"""Conflict detection between candidate memories and established knowledge."""

import logging
import re
from typing import Sequence

from src.database.models.memory import MemoryModel
from src.memory.schemas.memory import FactType, MemoryCandidate

logger = logging.getLogger(__name__)

# Mutually exclusive attribute pairs for heuristic conflict detection
_CONFLICT_PAIRS = [
    (r"\blives? in ([a-z\s]+)", r"\blives? in ([a-z\s]+)"),
    (r"\bfrom ([a-z\s]+)", r"\bfrom ([a-z\s]+)"),
    (r"\bfavorite ([a-z]+) is ([a-z0-9\s]+)", r"\bfavorite \1 is ([a-z0-9\s]+)"),
    (r"\buses? ([a-z0-9]+)\b", r"\bdoes not use ([a-z0-9]+)\b"),
    (r"\blikes? ([a-z0-9\s]+)\b", r"\bdislikes? ([a-z0-9\s]+)\b"),
]


class ConflictResult:
    """Encapsulates conflict detection analysis."""

    def __init__(
        self,
        has_conflict: bool,
        existing_memory: MemoryModel | None = None,
        reason: str | None = None,
        should_overwrite: bool = False,
    ) -> None:
        self.has_conflict = has_conflict
        self.existing_memory = existing_memory
        self.reason = reason
        self.should_overwrite = should_overwrite


class ConflictDetector:
    """Analyzes candidates against established memories to flag or resolve contradictions."""

    def detect_conflict(
        self,
        candidate: MemoryCandidate,
        existing_memories: Sequence[MemoryModel],
    ) -> ConflictResult:
        """Evaluate if candidate contradicts an established active memory."""
        cand_lower = candidate.content.lower()

        for existing in existing_memories:
            # Only compare memories in the same category
            if existing.category != candidate.category.value:
                continue

            exist_lower = existing.content.lower()

            # 1. Direct negation conflict: e.g. "likes python" vs "dislikes python"
            for pos_pat, neg_pat in _CONFLICT_PAIRS:
                m1 = re.search(pos_pat, cand_lower)
                m2 = re.search(neg_pat, exist_lower)
                if m1 and m2:
                    # If same topic but contradictory values
                    val1 = m1.group(1).strip()
                    val2 = m2.group(1).strip()
                    if val1 != val2:
                        reason = f"Candidate '{candidate.content}' contradicts established '{existing.content}'"
                        # Only overwrite if candidate is an explicit high-confidence fact and existing is lower or inference
                        should_overwrite = (
                            candidate.fact_type == FactType.EXPLICIT_FACT
                            and candidate.confidence >= existing.confidence
                            and existing.fact_type != FactType.EXPLICIT_FACT
                        )
                        return ConflictResult(
                            has_conflict=True,
                            existing_memory=existing,
                            reason=reason,
                            should_overwrite=should_overwrite,
                        )

            # 2. Key attribute conflict (e.g., both state "lives in X" with different cities)
            match_live_cand = re.search(r"\blive[sd]? in ([a-zA-Z\s]+)", cand_lower)
            match_live_exist = re.search(r"\blive[sd]? in ([a-zA-Z\s]+)", exist_lower)
            if match_live_cand and match_live_exist:
                loc1 = match_live_cand.group(1).strip()
                loc2 = match_live_exist.group(1).strip()
                if loc1 != loc2:
                    reason = f"Conflicting location: '{loc1}' vs '{loc2}'"
                    should_overwrite = (
                        candidate.fact_type == FactType.EXPLICIT_FACT
                        and existing.fact_type != FactType.EXPLICIT_FACT
                    )
                    return ConflictResult(
                        has_conflict=True,
                        existing_memory=existing,
                        reason=reason,
                        should_overwrite=should_overwrite,
                    )

        return ConflictResult(has_conflict=False)

