"""Repository managing PostgreSQL operations for conversations and chronological messages."""

from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models.base import utc_now
from src.database.models.conversation import ConversationModel, MessageModel

logger = logging.getLogger(__name__)


class ConversationRepository:
    """Manages persistent conversation sessions and message history in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_active_conversation(
        self,
        user_id: int,
        conversation_id: str | None = None,
    ) -> ConversationModel:
        """Fetch the active conversation for a user, or create one if none exists."""
        stmt = (
            select(ConversationModel)
            .where(
                ConversationModel.user_id == user_id,
                ConversationModel.is_active == True,  # noqa: E712
            )
            .order_by(desc(ConversationModel.updated_at))
        )
        result = await self.session.execute(stmt)
        conv = result.scalars().first()

        if conv is not None:
            return conv

        new_conv_id = conversation_id or f"conv_{uuid.uuid4().hex[:16]}"
        conv = ConversationModel(
            conversation_id=new_conv_id,
            user_id=user_id,
            is_active=True,
            message_count=0,
            metadata_extra={},
        )
        self.session.add(conv)
        await self.session.flush()
        logger.info("Created new PostgreSQL conversation: %s for user_id=%d", new_conv_id, user_id)
        return conv

    async def get_conversation(self, conversation_id: str, user_id: int | None = None) -> ConversationModel | None:
        """Fetch a conversation by ID, enforcing optional user isolation."""
        stmt = select(ConversationModel).where(ConversationModel.conversation_id == conversation_id)
        if user_id is not None:
            stmt = stmt.where(ConversationModel.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def add_message(
        self,
        conversation_id: str,
        user_id: int,
        role: str,
        content: str,
    ) -> MessageModel:
        """Append a message turn to a conversation and increment message count."""
        msg = MessageModel(
            conversation_id=conversation_id,
            user_id=user_id,
            role=role.strip().lower(),
            content=content.strip(),
        )
        self.session.add(msg)

        # Update parent conversation's message count and updated_at
        stmt = (
            update(ConversationModel)
            .where(ConversationModel.conversation_id == conversation_id)
            .values(
                message_count=ConversationModel.message_count + 1,
                updated_at=utc_now(),
            )
        )
        await self.session.execute(stmt)
        await self.session.flush()
        return msg

    async def get_recent_messages(
        self,
        conversation_id: str,
        user_id: int | None = None,
        limit: int = 50,
        order: str = "asc",
    ) -> list[MessageModel]:
        """Retrieve recent messages for a conversation.

        If order is 'asc', messages are returned in chronological order (oldest to newest).
        If order is 'desc', messages are returned newest first.
        """
        stmt = select(MessageModel).where(MessageModel.conversation_id == conversation_id)
        if user_id is not None:
            stmt = stmt.where(MessageModel.user_id == user_id)

        # Order newest first to apply limit window, then sort chronologically if order=='asc'
        stmt = stmt.order_by(desc(MessageModel.created_at)).limit(limit)
        result = await self.session.execute(stmt)
        messages = list(result.scalars().all())

        if order == "asc":
            messages.reverse()
        return messages

    async def update_summary(
        self,
        conversation_id: str,
        summary_payload: dict[str, Any],
    ) -> bool:
        """Update conversation summary metadata without touching original messages."""
        stmt = select(ConversationModel).where(ConversationModel.conversation_id == conversation_id)
        result = await self.session.execute(stmt)
        conv = result.scalars().first()
        if not conv:
            return False

        meta = dict(conv.metadata_extra or {})
        meta["summary"] = summary_payload
        conv.metadata_extra = meta
        conv.updated_at = utc_now()
        await self.session.flush()
        return True

    async def get_summary(self, conversation_id: str) -> dict[str, Any] | None:
        """Fetch cached conversation summary if available."""
        stmt = select(ConversationModel).where(ConversationModel.conversation_id == conversation_id)
        result = await self.session.execute(stmt)
        conv = result.scalars().first()
        if not conv or not conv.metadata_extra:
            return None
        return conv.metadata_extra.get("summary")

    async def clear_active_conversation(self, user_id: int) -> bool:
        """Mark active conversations as inactive."""
        stmt = (
            update(ConversationModel)
            .where(
                ConversationModel.user_id == user_id,
                ConversationModel.is_active == True,  # noqa: E712
            )
            .values(is_active=False, updated_at=utc_now())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

