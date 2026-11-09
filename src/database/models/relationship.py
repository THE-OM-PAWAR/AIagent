"""SQLAlchemy model for relationships between memories, projects, and decisions."""

from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MemoryRelationshipModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Represents a directional relationship between two memory entities."""

    __tablename__ = "memory_relationships"

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    source_memory_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_memory_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # e.g., memory_to_project, event_to_decision, task_to_project, supports, contradicts, related_to
    relationship_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    metadata_extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        Index("idx_mem_rel_user_source", "user_id", "source_memory_id"),
        Index("idx_mem_rel_user_target", "user_id", "target_memory_id"),
        Index("idx_mem_rel_user_type", "user_id", "relationship_type"),
    )

