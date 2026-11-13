"""Conflict detection between candidate memories and established knowledge."""

import logging
import re
from typing import Sequence

from src.database.models.memory import MemoryModel
from src.memory.schemas.memory import FactType, MemoryCandidate

logger = logging.getLogger(__name__)

# Mutually exclusive attribute pairs (contradiction occurs when different values are asserted for the same attribute)
_EXCLUSIVE_PAIRS = [
    (r"\blives?\s+in\s+([a-z\s]+)", r"\blives?\s+in\s+([a-z\s]+)"),
    (r"\bfrom\s+([a-z\s]+)", r"\bfrom\s+([a-z\s]+)"),
    (r"\bworks?\s+at\s+([a-z0-9\s]+)", r"\bworks?\s+at\s+([a-z0-9\s]+)"),
]

# Opposing polarity pairs (contradiction occurs when the SAME entity/subject has opposing sentiment or status)
_OPPOSING_PAIRS = [
    (r"\buses?\s+([a-z0-9]+)\b", r"\b(?:does not use|doesn't use|never uses)\s+([a-z0-9]+)\b"),
    (r"\b(?:likes?|loves?|prefers?)\s+([a-z0-9\s]+)\b", r"\b(?:dislikes?|hates?)\s+([a-z0-9\s]+)\b"),
]

_FAVORITE_PATTERN = re.compile(r"\bfavorite\s+([a-z]+)\s+is\s+([a-z0-9\s]+)")


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
        """Evaluate if candidate contradicts an established active memory.

        Completely isolated from crashing conversation flow.
        """
        try:
            cand_lower = candidate.content.lower().strip()

            for existing in existing_memories:
                # Only compare memories in the same category
                if existing.category != candidate.category.value:
                    continue

                exist_lower = existing.content.lower().strip()

                # 1. Favorite attribute conflict: e.g. "favorite color is blue" vs "favorite color is red"
                m_cand_fav = _FAVORITE_PATTERN.search(cand_lower)
                m_exist_fav = _FAVORITE_PATTERN.search(exist_lower)
                if m_cand_fav and m_exist_fav:
                    topic_cand = m_cand_fav.group(1).strip()
                    val_cand = m_cand_fav.group(2).strip()
                    topic_exist = m_exist_fav.group(1).strip()
                    val_exist = m_exist_fav.group(2).strip()

                    if topic_cand == topic_exist and val_cand != val_exist:
                        reason = f"Candidate '{candidate.content}' contradicts established '{existing.content}' on favorite {topic_cand}"
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

                # 2. Exclusive attribute conflict (e.g. different locations or origins)
                for pat1, pat2 in _EXCLUSIVE_PAIRS:
                    m1 = re.search(pat1, cand_lower)
                    m2 = re.search(pat2, exist_lower)
                    if m1 and m2:
                        val1 = m1.group(1).strip()
                        val2 = m2.group(1).strip()
                        if val1 != val2:
                            reason = f"Candidate '{candidate.content}' contradicts established '{existing.content}'"
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

                # 3. Opposing polarity conflict (e.g. "likes python" vs "dislikes python")
                for pos_pat, neg_pat in _OPPOSING_PAIRS:
                    # Check candidate (pos) vs existing (neg)
                    m_pos = re.search(pos_pat, cand_lower)
                    m_neg = re.search(neg_pat, exist_lower)
                    if m_pos and m_neg:
                        if m_pos.group(1).strip() == m_neg.group(1).strip():
                            reason = f"Candidate '{candidate.content}' contradicts established '{existing.content}'"
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

                    # Check candidate (neg) vs existing (pos)
                    m_neg_cand = re.search(neg_pat, cand_lower)
                    m_pos_exist = re.search(pos_pat, exist_lower)
                    if m_neg_cand and m_pos_exist:
                        if m_neg_cand.group(1).strip() == m_pos_exist.group(1).strip():
                            reason = f"Candidate '{candidate.content}' contradicts established '{existing.content}'"
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

                # 4. Fallback location conflict (e.g. "lived in Paris" vs "lives in Rome")
                match_live_cand = re.search(r"\blive[sd]?\s+in\s+([a-zA-Z\s]+)", cand_lower)
                match_live_exist = re.search(r"\blive[sd]?\s+in\s+([a-zA-Z\s]+)", exist_lower)
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
        except Exception as exc:
            logger.warning("ConflictDetector: unhandled exception in conflict detection (shielded): %s", exc)
            return ConflictResult(has_conflict=False)


