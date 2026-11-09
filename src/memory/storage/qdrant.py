"""Asynchronous Qdrant vector storage client with payload indexing and user isolation."""

import logging
from typing import Any
import uuid

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointIdsList,
    PointStruct,
    VectorParams,
)

logger = logging.getLogger(__name__)


class QdrantStorage:
    """Manages collection lifecycle, vector upsert, deletion, and dense search in Qdrant."""

    def __init__(
        self,
        url: str = ":memory:",
        api_key: str | None = None,
        collection_name: str = "aiagent_memories",
        vector_dim: int = 768,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.collection_name = collection_name
        self.vector_dim = vector_dim
        self._client: AsyncQdrantClient | None = None

    async def get_client(self) -> AsyncQdrantClient:
        """Return or instantiate AsyncQdrantClient."""
        if self._client is None:
            if self.url == ":memory:":
                logger.info("Initializing in-memory AsyncQdrantClient...")
                self._client = AsyncQdrantClient(location=":memory:")
            else:
                logger.info("Connecting to Qdrant at %s...", self.url)
                self._client = AsyncQdrantClient(url=self.url, api_key=self.api_key)
        return self._client

    async def initialize(self) -> None:
        """Ensure vector collection and filtering payload indexes exist."""
        client = await self.get_client()
        collections_response = await client.get_collections()
        existing = [c.name for c in collections_response.collections]

        if self.collection_name not in existing:
            logger.info("Creating Qdrant collection '%s' (dim: %d)...", self.collection_name, self.vector_dim)
            await client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=self.vector_dim, distance=Distance.COSINE),
            )

            # Create payload indexes for fast user-isolated filtered queries
            payload_fields = [
                ("user_id", PayloadSchemaType.INTEGER),
                ("category", PayloadSchemaType.KEYWORD),
                ("status", PayloadSchemaType.KEYWORD),
                ("project_association", PayloadSchemaType.KEYWORD),
            ]
            for field_name, field_schema in payload_fields:
                try:
                    await client.create_payload_index(
                        collection_name=self.collection_name,
                        field_name=field_name,
                        field_schema=field_schema,
                    )
                except Exception as exc:
                    logger.debug("Payload index for '%s' note: %s", field_name, exc)

        logger.info("Qdrant collection '%s' is ready.", self.collection_name)

    async def upsert_vector(
        self,
        memory_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None:
        """Upsert a single memory vector and its metadata payload into Qdrant."""
        client = await self.get_client()
        point = PointStruct(
            id=memory_id,
            vector=vector,
            payload=payload,
        )
        await client.upsert(
            collection_name=self.collection_name,
            points=[point],
        )
        logger.debug("Upserted vector point id=%s into Qdrant", memory_id)

    async def delete_vector(self, memory_id: str) -> None:
        """Delete a vector point from Qdrant by memory ID."""
        client = await self.get_client()
        await client.delete(
            collection_name=self.collection_name,
            points_selector=PointIdsList(points=[memory_id]),
        )
        logger.debug("Deleted vector point id=%s from Qdrant", memory_id)

    async def search_dense(
        self,
        user_id: int,
        query_vector: list[float],
        category: str | None = None,
        project_association: str | None = None,
        limit: int = 10,
        score_threshold: float = 0.0,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        """Perform user-isolated dense vector search in Qdrant.

        Returns:
            List of (memory_id, similarity_score, payload)
        """
        client = await self.get_client()

        must_conditions: list[Any] = [
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
            FieldCondition(key="status", match=MatchValue(value="active")),
        ]
        if category:
            must_conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
        if project_association:
            must_conditions.append(
                FieldCondition(key="project_association", match=MatchValue(value=project_association))
            )

        query_filter = Filter(must=must_conditions)

        results = await client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            score_threshold=score_threshold,
        )

        scored_points: list[tuple[str, float, dict[str, Any]]] = []
        for point in results.points:
            scored_points.append((str(point.id), float(point.score), point.payload or {}))

        return scored_points

    async def close(self) -> None:
        """Close Qdrant client connection."""
        if self._client is not None:
            await self._client.close()
            self._client = None

