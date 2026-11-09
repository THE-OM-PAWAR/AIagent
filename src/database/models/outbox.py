"""Transactional Outbox model for reliable vector synchronization with Qdrant."""

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MemoryOutboxModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Outbox queue table ensuring reliable synchronization between PostgreSQL and Qdrant."""

    __tablename__ = "memory_outbox"

    memory_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # Operations: insert, update, delete
    operation: Mapped[str] = mapped_column(String(16), nullable=False)

    # Status: pending, processing, completed, failed
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)

    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    memory = relationship("MemoryModel", back_populates="outbox_entries")

    __table_args__ = (
        Index("idx_outbox_status_retry", "status", "retry_count"),
        Index("idx_outbox_created", "created_at"),
    )

