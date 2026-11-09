"""Pydantic schemas for the unified memory architecture."""

from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, field_validator


class MemoryCategory(str, Enum):
    """The five primary memory categories."""
    SEMANTIC = "semantic"      # stable user facts and preferences
    EPISODIC = "episodic"      # important events, experiences, decisions
    PROJECT = "project"        # project details, architecture, technical choices
    TASK = "task"              # objectives, progress, blockers
    WORKING = "working"        # temporary execution information


class FactType(str, Enum):
    """Memory provenance and certainty classification."""
    EXPLICIT_FACT = "explicit_fact"  # stated directly by the user
    PLAN = "plan"                    # stated user intention or roadmap
    INFERENCE = "inference"          # deduced context


class MemoryStatus(str, Enum):
    """Lifecycle status of a memory."""
    ACTIVE = "active"
    ARCHIVED = "archived"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


class SourceReference(BaseModel):
    """Source provenance tracking."""
    conversation_id: str | None = None
    message_id: str | int | None = None
    timestamp: str | None = None
    excerpt: str | None = None


class MemoryCandidate(BaseModel):
    """Raw candidate memory extracted from conversational interactions."""
    category: MemoryCategory
    content: str = Field(..., min_length=2, max_length=4000)
    fact_type: FactType = FactType.EXPLICIT_FACT
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    project_association: str | None = None
    expires_at: datetime | None = None
    metadata_extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def clean_content(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Memory content cannot be empty.")
        return cleaned


class MemoryCreate(MemoryCandidate):
    """Validated memory payload ready for persistence."""
    user_id: int
    source_reference: SourceReference = Field(default_factory=SourceReference)


class MemoryUpdate(BaseModel):
    """Optional fields for updating an existing memory."""
    content: str | None = Field(default=None, min_length=2, max_length=4000)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    project_association: str | None = None
    status: MemoryStatus | None = None
    metadata_extra: dict[str, Any] | None = None


class MemoryRecord(BaseModel):
    """Complete structured memory entity."""
    id: str
    user_id: int
    category: MemoryCategory
    content: str
    source_reference: dict[str, Any]
    project_association: str | None = None
    importance: float
    confidence: float
    fact_type: FactType
    status: MemoryStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    metadata_extra: dict[str, Any] = Field(default_factory=dict)

    model_config = {"from_attributes": True}

