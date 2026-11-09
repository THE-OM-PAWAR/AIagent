"""Pydantic schemas for the RAG retrieval pipeline."""

from typing import Any
from pydantic import BaseModel, Field

from src.memory.schemas.memory import MemoryCategory, MemoryRecord, MemoryStatus


class RetrievalFilter(BaseModel):
    """Filters to constrain hybrid memory retrieval."""
    user_id: int
    categories: list[MemoryCategory] | None = None
    project_association: str | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    min_importance: float = Field(default=0.0, ge=0.0, le=1.0)


class RetrievalCandidate(BaseModel):
    """Candidate memory retrieved from a search component (dense or keyword)."""
    memory_id: str
    record: MemoryRecord
    source: str  # 'dense' or 'keyword'
    score: float
    rank: int


class RetrievalResult(BaseModel):
    """Final fused and reranked memory output for context integration."""
    memory_id: str
    category: MemoryCategory
    content: str
    importance: float
    confidence: float
    relevance_score: float
    project_association: str | None = None
    source_reference: dict[str, Any]
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)

