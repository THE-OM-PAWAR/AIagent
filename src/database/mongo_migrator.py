"""Safe, idempotent migration utility from MongoDB into PostgreSQL / Neon."""

from datetime import datetime, timezone
import logging
from typing import Any

from pymongo import AsyncMongoClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models.conversation import ConversationModel, MessageModel
from src.database.session import get_async_session
from src.memory.manager import MemoryManager

logger = logging.getLogger(__name__)


class MongoPostgresMigrator:
    """Migrates historical conversations, messages, and memories from MongoDB to PostgreSQL."""

    def __init__(
        self,
        mongodb_uri: str,
        mongodb_db_name: str,
        postgres_url: str,
        memory_manager: MemoryManager | None = None,
    ) -> None:
        self.mongodb_uri = mongodb_uri
        self.mongodb_db_name = mongodb_db_name
        self.postgres_url = postgres_url
        self.memory_manager = memory_manager

    async def run_migration(self, extract_memories: bool = True) -> dict[str, int]:
        """Execute full data migration from MongoDB to PostgreSQL without discarding history."""
        logger.info("Starting MongoDB to PostgreSQL migration...")
        mongo_client: AsyncMongoClient = AsyncMongoClient(self.mongodb_uri, serverSelectionTimeoutMS=5000)
        mongo_db = mongo_client[self.mongodb_db_name]

        stats = {
            "conversations_migrated": 0,
            "messages_migrated": 0,
            "memories_extracted": 0,
        }

        try:
            # 1. Migrate Conversations
            async with get_async_session(self.postgres_url) as session:
                cursor = mongo_db.conversations.find()
                async for conv_doc in cursor:
                    conv_id = str(conv_doc.get("conversation_id", ""))
                    if not conv_id:
                        continue

                    # Check if already migrated
                    stmt = select(ConversationModel).where(ConversationModel.conversation_id == conv_id)
                    exists = (await session.execute(stmt)).scalars().first()
                    if not exists:
                        created_at = conv_doc.get("created_at") or datetime.now(timezone.utc)
                        updated_at = conv_doc.get("updated_at") or created_at
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=timezone.utc)
                        if updated_at.tzinfo is None:
                            updated_at = updated_at.replace(tzinfo=timezone.utc)

                        model = ConversationModel(
                            conversation_id=conv_id,
                            user_id=int(conv_doc.get("user_id", 0)),
                            is_active=bool(conv_doc.get("is_active", True)),
                            message_count=int(conv_doc.get("message_count", 0)),
                            created_at=created_at,
                            updated_at=updated_at,
                            metadata_extra=conv_doc.get("metadata", {}),
                        )
                        session.add(model)
                        stats["conversations_migrated"] += 1

                await session.commit()

            # 2. Migrate Messages & Extract Salient Memories
            async with get_async_session(self.postgres_url) as session:
                cursor = mongo_db.messages.find().sort("created_at", 1)
                async for msg_doc in cursor:
                    conv_id = str(msg_doc.get("conversation_id", ""))
                    user_id = int(msg_doc.get("user_id", 0))
                    role = str(msg_doc.get("role", "user"))
                    content = str(msg_doc.get("content", ""))

                    if not conv_id or not content:
                        continue

                    # Store in PostgreSQL messages
                    created_at = msg_doc.get("created_at") or datetime.now(timezone.utc)
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)

                    msg_model = MessageModel(
                        conversation_id=conv_id,
                        user_id=user_id,
                        role=role,
                        content=content,
                        created_at=created_at,
                    )
                    session.add(msg_model)
                    stats["messages_migrated"] += 1

                    # Extract historical memories for user messages
                    if extract_memories and role == "user" and self.memory_manager:
                        try:
                            res = await self.memory_manager.remember_turn(
                                user_id=user_id,
                                user_text=content,
                                conversation_id=conv_id,
                            )
                            stats["memories_extracted"] += len(res.created)
                        except Exception as exc:
                            logger.debug("Non-fatal memory extraction error during migration: %s", exc)

                await session.commit()

            logger.info("Migration completed successfully: %s", stats)
        finally:
            await mongo_client.close()

        return stats

