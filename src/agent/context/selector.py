"""Context item selection, deduplication, conflict suppression, and user/project isolation."""

import logging
import re
from typing import Any
from pydantic import BaseModel, Field

from src.agent.context.ranking import ContextRanker, ScoredContextItem
from src.memory.schemas.memory import MemoryCategory
from src.memory.schemas.retrieval import RetrievalResult

logger = logging.getLogger(__name__)


class SelectionResult(BaseModel):
    """Result of selecting, deduplicating, and isolating context items."""
    selected_items: list[ScoredContextItem] = Field(default_factory=list)
    semantic_items: list[ScoredContextItem] = Field(default_factory=list)
    project_items: list[ScoredContextItem] = Field(default_factory=list)
    task_items: list[ScoredContextItem] = Field(default_factory=list)
    deduplicated_count: int = 0
    cross_user_rejections: int = 0
    cross_project_rejections: int = 0
    explanations: list[str] = Field(default_factory=list)


class ContextSelector:
    """Selects and filters memories and context items for optimal prompt integration."""

    def __init__(
        self,
        ranker: ContextRanker | None = None,
        duplicate_similarity_threshold: float = 0.80,
    ) -> None:
        self.ranker = ranker or ContextRanker()
        self.duplicate_similarity_threshold = duplicate_similarity_threshold

    @staticmethod
    def tokenize_content(text: str) -> set[str]:
        """Normalize and tokenize text into words for similarity comparisons."""
        cleaned = re.sub(r"[^\w\s]", " ", text.lower())
        return set(cleaned.split())

    def compute_jaccard_similarity(self, tokens_a: set[str], tokens_b: set[str]) -> float:
        """Calculate Jaccard token overlap between two texts."""
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = len(tokens_a & tokens_b)
        union = len(tokens_a | tokens_b)
        return intersection / union if union > 0 else 0.0

    def select_context(
        self,
        user_id: int,
        retrieved_results: list[RetrievalResult],
        recent_messages: list[Any],
        active_project: str | None = None,
        max_items: int = 15,
    ) -> SelectionResult:
        """Filter, deduplicate, and partition retrieved memories across context sources."""
        result = SelectionResult()

        # Extract recent conversation tokens for cross-source deduplication
        recent_text_tokens: list[set[str]] = []
        for msg in recent_messages:
            content = getattr(msg, "content", "") if not isinstance(msg, dict) else msg.get("content", "")
            if content:
                recent_text_tokens.append(self.tokenize_content(content))

        # 1. User & Project Isolation Filtering
        valid_results: list[RetrievalResult] = []
        for r in retrieved_results:
            # Check user isolation (via metadata or record if present)
            item_user_id = r.metadata.get("user_id")
            if item_user_id is not None and int(item_user_id) != int(user_id):
                result.cross_user_rejections += 1
                logger.warning(
                    "ContextSelector: Cross-user memory detected and rejected! Current user=%s, memory user=%s",
                    user_id,
                    item_user_id,
                )
                continue

            # Check project isolation:
            # If active_project is given, items explicitly associated with a different project are skipped
            # (Unless they are general semantic facts, e.g. language/editor preference)
            cat_str = r.category.value if hasattr(r.category, "value") else str(r.category)
            if active_project and r.project_association:
                if (
                    r.project_association.strip().lower() != active_project.strip().lower()
                    and cat_str in {MemoryCategory.PROJECT.value, MemoryCategory.TASK.value}
                ):
                    result.cross_project_rejections += 1
                    logger.debug(
                        "ContextSelector: Project mismatch for memory %s: item_proj=%s, active_proj=%s",
                        r.memory_id,
                        r.project_association,
                        active_project,
                    )
                    continue

            valid_results.append(r)

        # 2. Score and Rank candidates
        scored_items = self.ranker.rank_items(valid_results, active_project=active_project)

        # 3. Deduplicate across candidates and against recent messages
        selected: list[ScoredContextItem] = []
        seen_token_sets: list[set[str]] = []

        for item in scored_items:
            item_tokens = self.tokenize_content(item.content)
            if not item_tokens:
                continue

            # Check if this memory is already stated almost verbatim in the recent conversation turns
            redundant_with_history = False
            for hist_tokens in recent_text_tokens:
                if self.compute_jaccard_similarity(item_tokens, hist_tokens) >= 0.85:
                    redundant_with_history = True
                    break

            if redundant_with_history and not item.is_protected:
                result.deduplicated_count += 1
                result.explanations.append(
                    f"Suppressed redundant memory '{item.content[:40]}...' (already in recent history)"
                )
                continue

            # Check duplicate against already selected items
            is_dup = False
            for existing_tokens in seen_token_sets:
                sim = self.compute_jaccard_similarity(item_tokens, existing_tokens)
                if sim >= self.duplicate_similarity_threshold:
                    is_dup = True
                    break

            if is_dup:
                result.deduplicated_count += 1
                result.explanations.append(
                    f"Deduplicated near-identical memory '{item.content[:40]}...'"
                )
                continue

            # Item accepted
            selected.append(item)
            seen_token_sets.append(item_tokens)
            reason = "Protected decision/constraint" if item.is_protected else f"Score: {item.final_score:.2f}"
            result.explanations.append(f"Included memory [{item.category}]: '{item.content[:40]}...' ({reason})")

            if len(selected) >= max_items:
                break

        result.selected_items = selected

        # Partition into context categories
        for item in selected:
            cat = item.category.lower()
            if cat in {MemoryCategory.SEMANTIC.value, MemoryCategory.EPISODIC.value}:
                result.semantic_items.append(item)
            elif cat == MemoryCategory.PROJECT.value:
                result.project_items.append(item)
            elif cat in {MemoryCategory.TASK.value, MemoryCategory.WORKING.value}:
                result.task_items.append(item)
            else:
                result.semantic_items.append(item)

        return result

