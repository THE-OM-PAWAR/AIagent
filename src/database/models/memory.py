"""SQLAlchemy memory model representing persistent user memory records."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, Index, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MemoryModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """PostgreSQL model for unified user memory records."""

    __tablename__ = "memories"

    # User isolation - strict foreign ownership
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # 5 Categories: semantic, episodic, project, task, working
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # Core textual content
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Source provenance (conversation_id, message_id, timestamp, excerpt)
    source_reference: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    # Optional project association tag
    project_association: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # Scoring metrics (0.0 - 1.0)
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    # Ingestion classification: explicit_fact, plan, inference
    fact_type: Mapped[str] = mapped_column(String(32), default="explicit_fact", nullable=False)

    # Lifecycle status: active, archived, invalidated, expired
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)

    # Expiration for temporary working memories
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # Extended structured metadata
    metadata_extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    # Outbox tasks relation
    outbox_entries = relationship("MemoryOutboxModel", back_populates="memory", cascade="all, delete-orphan")

    # Composite indexes for high-speed filtered queries
    __table_args__ = (
        Index("idx_memories_user_status", "user_id", "status"),
        Index("idx_memories_user_category", "user_id", "category"),
        Index("idx_memories_user_project", "user_id", "project_association"),
        Index("idx_memories_created_at", "created_at"),
    )

