"""Asynchronous MongoDB client with collections for conversations and messages."""

from datetime import datetime, timezone, timedelta
import logging
import re
from typing import Any
import uuid

from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

logger = logging.getLogger(__name__)


def mask_mongo_uri(uri: str) -> str:
    """Mask credentials in MongoDB connection URI for secure logging.

    Args:
        uri: Raw MongoDB connection string.

    Returns:
        Sanitized connection string with masked password and username.
    """
    return re.sub(r"://([^:]+):([^@]+)@", r"://***:***@", uri)


class DatabaseClient:
    """Manages asynchronous MongoDB operations for conversations and message history."""

    def __init__(self, uri: str, db_name: str) -> None:
        self.uri = uri
        self.db_name = db_name
        self.masked_uri = mask_mongo_uri(uri)
        self._client: AsyncMongoClient | None = None
        self._db: AsyncDatabase | None = None

    async def connect(self) -> None:
        """Initialize AsyncMongoClient, test connection with ping, and ensure indexes.

        Raises:
            Exception: If the database is unreachable or credentials fail.
        """
        logger.info("Connecting to MongoDB at %s (database: %s)...", self.masked_uri, self.db_name)
        self._client = AsyncMongoClient(self.uri, serverSelectionTimeoutMS=5000)
        self._db = self._client[self.db_name]

        try:
            # Verify connectivity using ping command on application database
            await self._db.command("ping")
            logger.info("Connected to MongoDB successfully.")

            # Ensure collection indexes
            await self.init_indexes()
        except Exception:
            if self._client is not None:
                await self._client.close()
                self._client = None
                self._db = None
            raise

    async def close(self) -> None:
        """Close AsyncMongoClient connection."""
        if self._client is not None:
            logger.info("Closing MongoDB connection to %s...", self.masked_uri)
            await self._client.close()
            self._client = None
            self._db = None
            logger.info("MongoDB connection closed.")

    @property
    def db(self) -> AsyncDatabase:
        """Return the active database instance."""
        if self._db is None:
            raise RuntimeError("DatabaseClient is not connected. Call await connect() first.")
        return self._db

    @property
    def conversations(self) -> AsyncCollection:
        """Return the conversations metadata collection."""
        return self.db["conversations"]

    @property
    def messages(self) -> AsyncCollection:
        """Return the messages collection."""
        return self.db["messages"]

    async def init_indexes(self) -> None:
        """Create necessary indexes on messages and conversations collections."""
        logger.info("Ensuring database indexes...")

        # Indexes for messages:
        # 1. Compound index for fast chronological history retrieval by conversation
        await self.messages.create_index(
            [("conversation_id", 1), ("created_at", 1)],
            name="idx_conv_created",
        )
        # 2. Index for queries by user across time
        await self.messages.create_index(
            [("user_id", 1), ("created_at", -1)],
            name="idx_user_created",
        )

        # Indexes for conversations:
        # 1. Unique index on conversation identifier
        await self.conversations.create_index(
            [("conversation_id", 1)],
            unique=True,
            name="idx_conv_id_unique",
        )
        # 2. Index for finding the user's active conversation
        await self.conversations.create_index(
            [("user_id", 1), ("is_active", 1), ("updated_at", -1)],
            name="idx_user_active_updated",
        )

        logger.info("Database indexes ensured successfully.")

    async def get_or_create_active_conversation(self, user_id: int) -> dict[str, Any]:
        """Retrieve existing active conversation document for user, or create a new one.

        Args:
            user_id: Telegram user ID.

        Returns:
            Dictionary representing the conversation metadata document.
        """
        conv = await self.conversations.find_one(
            {"user_id": user_id, "is_active": True},
            sort=[("updated_at", -1)],
        )
        if conv:
            return conv

        now = datetime.now(timezone.utc)
        conv_id = uuid.uuid4().hex
        new_conv = {
            "conversation_id": conv_id,
            "user_id": user_id,
            "is_active": True,
            "message_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        await self.conversations.insert_one(new_conv)
        logger.info("Created new active conversation %s for user %s", conv_id, user_id)
        return new_conv

    async def clear_active_conversation(self, user_id: int) -> bool:
        """Mark all active conversations for a user as inactive.

        Args:
            user_id: Telegram user ID.

        Returns:
            True if any active conversation was marked inactive, False otherwise.
        """
        now = datetime.now(timezone.utc)
        result = await self.conversations.update_many(
            {"user_id": user_id, "is_active": True},
            {"$set": {"is_active": False, "updated_at": now}},
        )
        cleared = result.modified_count > 0
        if cleared:
            logger.info("Archived active conversation(s) for user %s", user_id)
        return cleared

    async def get_recent_messages(
        self,
        conversation_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Retrieve the most recent messages for a conversation in chronological order.

        Args:
            conversation_id: Unique conversation identifier.
            limit: Maximum number of recent messages to return.

        Returns:
            List of message dictionaries sorted by created_at ascending (oldest first).
        """
        cursor = self.messages.find(
            {"conversation_id": conversation_id},
            {"_id": 0, "role": 1, "content": 1, "created_at": 1},
        ).sort("created_at", -1).limit(limit)

        docs = await cursor.to_list(length=limit)
        # Reverse descending query results to chronological ascending order (oldest first)
        docs.reverse()
        return docs

    async def save_turn(
        self,
        conversation_id: str,
        user_id: int,
        user_message: str,
        model_response: str,
    ) -> None:
        """Persist a user message and model response turn into MongoDB.

        Args:
            conversation_id: Unique conversation identifier.
            user_id: Telegram user ID.
            user_message: User input content.
            model_response: Generated AI response content.
        """
        now = datetime.now(timezone.utc)
        user_doc = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "role": "user",
            "content": user_message,
            "created_at": now,
        }
        model_doc = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "role": "model",
            "content": model_response,
            "created_at": now + timedelta(microseconds=1),  # ensure distinct ordering
        }

        await self.messages.insert_many([user_doc, model_doc])

        await self.conversations.update_one(
            {"conversation_id": conversation_id},
            {
                "$set": {"updated_at": model_doc["created_at"]},
                "$inc": {"message_count": 2},
            },
        )
        logger.debug("Persisted turn to conversation %s for user %s", conversation_id, user_id)
