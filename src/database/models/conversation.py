"""SQLAlchemy models for persistent conversations and message history."""

from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ConversationModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Stores conversation metadata in PostgreSQL."""

    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    messages = relationship("MessageModel", back_populates="conversation", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_conversations_user_active", "user_id", "is_active", "updated_at"),
    )


class MessageModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Stores individual message turns in PostgreSQL."""

    __tablename__ = "messages"

    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    conversation = relationship("ConversationModel", back_populates="messages")

    __table_args__ = (
        Index("idx_messages_conv_created", "conversation_id", "created_at"),
        Index("idx_messages_user_created", "user_id", "created_at"),
    )

