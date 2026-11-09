"""SQLAlchemy model for recording memory conflicts."""

from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MemoryConflictModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Stores detected conflicts between candidate information and established memories."""

    __tablename__ = "memory_conflicts"

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    existing_memory_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    conflicting_content: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # Status: unresolved, resolved, dismissed
    status: Mapped[str] = mapped_column(String(32), default="unresolved", nullable=False, index=True)

    source_reference: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        Index("idx_conflicts_user_status", "user_id", "status"),
    )

