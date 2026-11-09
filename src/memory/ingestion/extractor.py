"""Memory extractor parsing conversation turns into structured memory candidates."""

import json
import logging
import re
from typing import Any

from src.memory.schemas.memory import FactType, MemoryCandidate, MemoryCategory

logger = logging.getLogger(__name__)

# Heuristic indicator patterns for fallback/offline extraction
_FACT_PATTERNS = [
    (r"\b(?:my name is|i am|call me)\s+([A-Z][a-z]+)", MemoryCategory.SEMANTIC, FactType.EXPLICIT_FACT, 0.9),
    (r"\b(?:i live in|i am from|my city is)\s+([A-Za-z\s]+)", MemoryCategory.SEMANTIC, FactType.EXPLICIT_FACT, 0.8),
    (r"\b(?:i prefer|i like|my favorite\s+\w+\s+is)\s+([A-Za-z0-9\s]+)", MemoryCategory.SEMANTIC, FactType.EXPLICIT_FACT, 0.7),
    (r"\b(?:i decided to|we chose|we agreed on)\s+([A-Za-z0-9\s]+)", MemoryCategory.EPISODIC, FactType.EXPLICIT_FACT, 0.8),
    (r"\b(?:i need to|i will|my goal is to|todo:?)\s+([A-Za-z0-9\s]+)", MemoryCategory.TASK, FactType.PLAN, 0.7),
    (r"\b(?:the project|our stack|architecture is)\s+([A-Za-z0-9\s]+)", MemoryCategory.PROJECT, FactType.EXPLICIT_FACT, 0.8),
]


class MemoryExtractor:
    """Extracts, categorizes, and validates candidate memories from text."""

    def __init__(self, gemini_client: Any = None) -> None:
        self.gemini_client = gemini_client

    async def extract_candidates(
        self,
        text: str,
        user_id: int | None = None,
    ) -> list[MemoryCandidate]:
        """Extract memory candidates from message text using LLM or rule heuristics."""
        clean = text.strip()
        if len(clean) < 5:
            return []

        # If LLM client is available and initialized, attempt structured extraction
        if self.gemini_client is not None and hasattr(self.gemini_client, "client"):
            try:
                extracted = await self._extract_with_llm(clean)
                if extracted:
                    return extracted
            except Exception as exc:
                logger.warning("LLM extraction error, falling back to rule extraction: %s", exc)

        return self._extract_with_rules(clean)

    def _extract_with_rules(self, text: str) -> list[MemoryCandidate]:
        """Extract candidate memories using pattern heuristics."""
        candidates: list[MemoryCandidate] = []
        lower = text.lower()

        # Check explicit regex patterns
        matched = False
        for pattern, category, fact_type, importance in _FACT_PATTERNS:
            if re.search(pattern, lower):
                candidates.append(
                    MemoryCandidate(
                        category=category,
                        content=text,
                        fact_type=fact_type,
                        importance=importance,
                        confidence=0.9,
                    )
                )
                matched = True
                break

        # If significant declarative sentence without trigger pattern, categorize by keywords
        if not matched and len(text) > 15:
            if any(w in lower for w in ["decided", "event", "happened", "yesterday"]):
                category = MemoryCategory.EPISODIC
            elif any(w in lower for w in ["todo", "task", "finish", "deadline", "blocker"]):
                category = MemoryCategory.TASK
            elif any(w in lower for w in ["database", "backend", "frontend", "architecture", "neon", "qdrant"]):
                category = MemoryCategory.PROJECT
            elif any(w in lower for w in ["temp", "currently", "now", "running"]):
                category = MemoryCategory.WORKING
            else:
                category = MemoryCategory.SEMANTIC

            candidates.append(
                MemoryCandidate(
                    category=category,
                    content=text,
                    fact_type=FactType.INFERENCE,
                    importance=0.5,
                    confidence=0.7,
                )
            )

        return candidates

    async def _extract_with_llm(self, text: str) -> list[MemoryCandidate]:
        """Perform structured extraction using Gemini."""
        prompt = (
            "Analyze the following user statement and extract any long-term valuable facts, decisions, "
            "tasks, or project details as JSON. If nothing is worth remembering, return empty array [].\n"
            "Format: Array of objects with keys: 'category' (one of: semantic, episodic, project, task, working), "
            "'content' (the clean summary fact), 'fact_type' (explicit_fact, plan, inference), "
            "'importance' (0.0 to 1.0), 'confidence' (0.0 to 1.0), 'project_association' (string or null).\n"
            f"User text: \"{text}\"\n"
            "JSON:"
        )
        response_text = await self.gemini_client.generate_response(user_id=0, message_text=prompt)
        # Parse JSON from response
        json_match = re.search(r"\[.*\]", response_text, re.DOTALL)
        if not json_match:
            return []

        raw_list = json.loads(json_match.group(0))
        candidates: list[MemoryCandidate] = []
        for item in raw_list:
            try:
                candidates.append(MemoryCandidate(**item))
            except Exception as exc:
                logger.debug("Failed parsing candidate item: %s (%s)", item, exc)

        return candidates

