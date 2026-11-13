"""Unit tests for memory extraction regex, conflict detection, and isolation."""

import unittest

from src.database.models.memory import MemoryModel
from src.memory.ingestion.conflict import ConflictDetector, ConflictResult
from src.memory.schemas.memory import FactType, MemoryCandidate, MemoryCategory


class TestMemoryConflictRegex(unittest.TestCase):
    """Test suite covering favorite regex capture groups, negation pairs, and exception shielding."""

    def setUp(self) -> None:
        self.detector = ConflictDetector()

    def test_favorite_regex_conflict_same_topic_different_value(self) -> None:
        """Verify that favorite attribute with same topic and differing values triggers conflict without regex errors."""
        existing = [
            MemoryModel(
                id="mem-fav-1",
                user_id=101,
                category=MemoryCategory.SEMANTIC.value,
                content="My favorite color is blue",
                fact_type=FactType.INFERENCE.value,
                confidence=0.6,
                status="active",
            )
        ]

        # Candidate asserts a different favorite color with explicit fact
        candidate = MemoryCandidate(
            category=MemoryCategory.SEMANTIC,
            content="My favorite color is red",
            fact_type=FactType.EXPLICIT_FACT,
            confidence=0.95,
        )

        res: ConflictResult = self.detector.detect_conflict(candidate, existing)
        self.assertTrue(res.has_conflict)
        self.assertIn("favorite color", res.reason)
        # Higher confidence explicit fact should overwrite inference
        self.assertTrue(res.should_overwrite)

    def test_favorite_regex_different_topics_no_conflict(self) -> None:
        """Verify that favorite attributes with different topics (color vs food) do NOT conflict."""
        existing = [
            MemoryModel(
                id="mem-fav-2",
                user_id=101,
                category=MemoryCategory.SEMANTIC.value,
                content="My favorite color is blue",
                fact_type=FactType.EXPLICIT_FACT.value,
                confidence=0.9,
                status="active",
            )
        ]

        candidate = MemoryCandidate(
            category=MemoryCategory.SEMANTIC,
            content="My favorite food is pizza",
            fact_type=FactType.EXPLICIT_FACT,
            confidence=0.9,
        )

        res: ConflictResult = self.detector.detect_conflict(candidate, existing)
        self.assertFalse(res.has_conflict)

    def test_location_conflict(self) -> None:
        """Verify that contradictory locations trigger conflict."""
        existing = [
            MemoryModel(
                id="mem-loc-1",
                user_id=101,
                category=MemoryCategory.SEMANTIC.value,
                content="User lives in London",
                fact_type=FactType.EXPLICIT_FACT.value,
                confidence=0.9,
                status="active",
            )
        ]

        candidate = MemoryCandidate(
            category=MemoryCategory.SEMANTIC,
            content="User lives in Tokyo",
            fact_type=FactType.INFERENCE,
            confidence=0.5,
        )

        res: ConflictResult = self.detector.detect_conflict(candidate, existing)
        self.assertTrue(res.has_conflict)
        # Inference should not overwrite explicit fact
        self.assertFalse(res.should_overwrite)

    def test_opposing_pairs_likes_dislikes(self) -> None:
        """Verify opposing polarity pairs (likes vs dislikes) detected bidirectionally."""
        existing = [
            MemoryModel(
                id="mem-like-1",
                user_id=101,
                category=MemoryCategory.SEMANTIC.value,
                content="User likes python",
                fact_type=FactType.EXPLICIT_FACT.value,
                confidence=0.9,
                status="active",
            )
        ]

        # Candidate expresses dislike
        cand1 = MemoryCandidate(
            category=MemoryCategory.SEMANTIC,
            content="User dislikes python",
            fact_type=FactType.EXPLICIT_FACT,
            confidence=0.9,
        )
        res1 = self.detector.detect_conflict(cand1, existing)
        self.assertTrue(res1.has_conflict)

        # Reverse direction: existing is dislike, candidate is likes
        existing_dislike = [
            MemoryModel(
                id="mem-dislike-1",
                user_id=101,
                category=MemoryCategory.SEMANTIC.value,
                content="User dislikes docker",
                fact_type=FactType.EXPLICIT_FACT.value,
                confidence=0.8,
                status="active",
            )
        ]
        cand2 = MemoryCandidate(
            category=MemoryCategory.SEMANTIC,
            content="User likes docker",
            fact_type=FactType.EXPLICIT_FACT,
            confidence=0.9,
        )
        res2 = self.detector.detect_conflict(cand2, existing_dislike)
        self.assertTrue(res2.has_conflict)

    def test_uses_vs_does_not_use_conflict(self) -> None:
        """Verify uses vs does not use contradiction is flagged."""
        existing = [
            MemoryModel(
                id="mem-use-1",
                user_id=101,
                category=MemoryCategory.SEMANTIC.value,
                content="User uses PostgreSQL",
                fact_type=FactType.EXPLICIT_FACT.value,
                confidence=0.9,
                status="active",
            )
        ]

        cand = MemoryCandidate(
            category=MemoryCategory.SEMANTIC,
            content="User does not use postgresql",
            fact_type=FactType.EXPLICIT_FACT,
            confidence=0.9,
        )
        res = self.detector.detect_conflict(cand, existing)
        self.assertTrue(res.has_conflict)

    def test_edge_cases_and_isolation(self) -> None:
        """Verify invalid group reference edge cases, regex metacharacters, and special chars never crash."""
        malformed_inputs = [
            r"Text with \1 backref pattern",
            r"Regex symbols (.*)+? [a-z]{",
            "ab",
            "   abc   ",
            "Special characters: !@#$%^&*()_+~`|}{[]:;?><,./-=",
            "Unicode test: 日本語 / 🚀 / Éléphant",
        ]

        existing = [
            MemoryModel(
                id="mem-safe-1",
                user_id=101,
                category=MemoryCategory.SEMANTIC.value,
                content="Sample standard memory",
                fact_type=FactType.EXPLICIT_FACT.value,
                confidence=0.8,
                status="active",
            )
        ]

        for text in malformed_inputs:
            candidate = MemoryCandidate(
                category=MemoryCategory.SEMANTIC,
                content=text,
                fact_type=FactType.INFERENCE,
                confidence=0.5,
            )
            # Must safely return ConflictResult without raising any exception
            res = self.detector.detect_conflict(candidate, existing)
            self.assertIsInstance(res, ConflictResult)
