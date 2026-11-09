"""Duplicate and near-duplicate memory detection."""

import re
from typing import Sequence

from src.database.models.memory import MemoryModel
from src.memory.schemas.memory import MemoryCandidate


def normalize_text(text: str) -> str:
    """Normalize text by lowercasing, removing punctuation and extra whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    return " ".join(text.split())


def calculate_jaccard_similarity(text1: str, text2: str) -> float:
    """Compute token-level Jaccard similarity coefficient between two strings."""
    tokens1 = set(normalize_text(text1).split())
    tokens2 = set(normalize_text(text2).split())
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1.intersection(tokens2)
    union = tokens1.union(tokens2)
    return len(intersection) / len(union)


class DuplicateDetector:
    """Detects exact duplicates and near-duplicate memories."""

    def __init__(self, near_duplicate_threshold: float = 0.85) -> None:
        self.threshold = near_duplicate_threshold

    def find_duplicate(
        self,
        candidate: MemoryCandidate,
        existing_memories: Sequence[MemoryModel],
    ) -> MemoryModel | None:
        """Return matching existing memory if candidate is an exact or near duplicate."""
        candidate_norm = normalize_text(candidate.content)

        for existing in existing_memories:
            existing_norm = normalize_text(existing.content)
            # 1. Exact match
            if candidate_norm == existing_norm:
                return existing

            # 2. Near duplicate check within the same category
            if candidate.category.value == existing.category:
                sim = calculate_jaccard_similarity(candidate.content, existing.content)
                if sim >= self.threshold:
                    return existing

        return None

