"""Base search provider interface and result models."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import html
import re
from typing import Any

from src.integrations.base import ConnectionHealth


@dataclass
class SearchResultItem:
    """Individual search result item."""
    title: str
    url: str
    snippet: str
    score: float = 0.0
    published_date: str | None = None
    source: str = "web"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "score": round(self.score, 3) if self.score else 0.0,
            "published_date": self.published_date,
            "source": self.source,
        }


def sanitize_web_snippet(text: str, max_chars: int = 1200) -> str:
    """Sanitize snippet text by stripping raw HTML and neutralizing prompt injection vectors."""
    if not text:
        return ""

    # Replace block level tags with spaces, inline tags with empty strings
    clean = re.sub(r"<(?:p|br|div|li|tr|h[1-6])\b[^>]*>", " ", text, flags=re.IGNORECASE)
    clean = re.sub(r"</(?:p|div|li|tr|h[1-6])>", " ", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", "", clean)
    clean = html.unescape(clean)

    # Neutralize prompt injection phrases
    suspicious_patterns = [
        re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions?", re.IGNORECASE),
        re.compile(r"system\s*(?:alert)?\s*:\s*(?:you\s+are|ignore)", re.IGNORECASE),
        re.compile(r"you\s+are\s+now\s+(?:an?\s+)?unrestricted", re.IGNORECASE),
        re.compile(r"disregard\s+prior\s+context", re.IGNORECASE),
    ]
    for pat in suspicious_patterns:
        clean = pat.sub("[Filtered Prompt-Injection Attempt]", clean)

    # Clean whitespace and normalize spaces around punctuation
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = re.sub(r"\s+([.,;:!?])", r"\1", clean)
    if len(clean) > max_chars:
        clean = clean[:max_chars] + "..."
    return clean


class BaseSearchProvider(ABC):
    """Abstract search provider (e.g. Tavily, Brave)."""

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 5,
        topic: str = "general",
        include_domains: list[str] | None = None,
    ) -> list[SearchResultItem]:
        """Execute web search returning sanitized SearchResultItem list."""
        pass

    @abstractmethod
    async def test_connection(self) -> ConnectionHealth:
        """Verify API key validity against provider."""
        pass
