"""Comprehensive unit and integration tests for Phase 1 Memory Foundation and RAG Pipeline."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database.models import (
    Base,
    ConversationModel,
    MemoryConflictModel,
    MemoryModel,
    MemoryOutboxModel,
    MemoryRelationshipModel,
    MessageModel,
)
from src.database.repositories.memory_repo import MemoryRepository
from src.database.repositories.outbox_repo import OutboxRepository
from src.database.repositories.relationship_repo import RelationshipRepository
from src.memory.embeddings.provider import EmbeddingError, EmbeddingProvider
from src.memory.ingestion.conflict import ConflictDetector
from src.memory.ingestion.deduplication import DuplicateDetector, calculate_jaccard_similarity
from src.memory.ingestion.extractor import MemoryExtractor
from src.memory.ingestion.pipeline import IngestionPipeline
from src.memory.manager import MemoryManager
from src.memory.relationships.manager import RelationshipManager
from src.memory.retrieval.dense import DenseRetriever
from src.memory.retrieval.fusion import ReciprocalRankFusion
from src.memory.retrieval.keyword import KeywordRetriever
from src.memory.retrieval.pipeline import RetrievalPipeline
from src.memory.retrieval.reranker import MemoryReranker
from src.memory.schemas.memory import (
    FactType,
    MemoryCandidate,
    MemoryCategory,
    MemoryRecord,
    MemoryStatus,
    MemoryUpdate,
    SourceReference,
)
from src.memory.schemas.retrieval import RetrievalCandidate, RetrievalFilter
from src.memory.storage.outbox import OutboxSynchronizer
from src.memory.storage.qdrant import QdrantStorage


class TestMemoryFoundationAndRAG(unittest.IsolatedAsyncioTestCase):
    """Integration and unit tests verifying the 12 Phase 1 completion criteria."""

    async def asyncSetUp(self) -> None:
        # Use in-memory SQLite async engine for isolated test database transactions
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.sessionmaker = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

        # In-memory Qdrant storage
        self.qdrant = QdrantStorage(
            url=":memory:",
            collection_name="test_memories",
            vector_dim=32,
        )
        await self.qdrant.initialize()

        # Embedding provider with 32-dim synthetic embeddings for fast offline testing
        self.embedding = EmbeddingProvider(
            api_key="test_offline_key",
            model="text-embedding-004",
            dimension=32,
        )
        self.embedding.initialize()

    async def asyncTearDown(self) -> None:
        await self.qdrant.close()
        await self.engine.dispose()

    # --------------------------------------------------------------------------
    # 1. Schema, Models and Pydantic Validation
    # --------------------------------------------------------------------------
    async def test_01_schema_and_validation(self) -> None:
        """Verify schema constraints, category enums, and Pydantic validation."""
        # Valid candidate across 5 categories
        for cat in [
            MemoryCategory.SEMANTIC,
            MemoryCategory.EPISODIC,
            MemoryCategory.PROJECT,
            MemoryCategory.TASK,
            MemoryCategory.WORKING,
        ]:
            cand = MemoryCandidate(
                category=cat,
                content=f"Important info for {cat.value}",
                importance=0.8,
                confidence=0.95,
            )
            self.assertEqual(cand.category, cat)

        # Content validation: empty content rejected
        with self.assertRaises(ValueError):
            MemoryCandidate(category=MemoryCategory.SEMANTIC, content="   ")

        # Database model persistence
        async with self.sessionmaker() as session:
            repo = MemoryRepository(session)
            mem = await repo.create_memory(
                user_id=1001,
                category="semantic",
                content="User prefers dark mode and Python.",
                importance=0.9,
                confidence=1.0,
            )
            await session.commit()
            self.assertIsNotNone(mem.id)
            self.assertEqual(mem.user_id, 1001)
            self.assertEqual(mem.status, "active")

    # --------------------------------------------------------------------------
    # 2. Memory Extraction
    # --------------------------------------------------------------------------
    async def test_02_memory_extraction(self) -> None:
        """Verify extraction rules and fact vs plan vs inference distinction."""
        extractor = MemoryExtractor()

        # Explicit fact
        cands1 = await extractor.extract_candidates("My name is Alice and I live in Paris")
        self.assertTrue(len(cands1) >= 1)
        self.assertEqual(cands1[0].category, MemoryCategory.SEMANTIC)
        self.assertEqual(cands1[0].fact_type, FactType.EXPLICIT_FACT)

        # Plan / Task
        cands2 = await extractor.extract_candidates("TODO: Finish migrating MongoDB to Neon PostgreSQL")
        self.assertTrue(len(cands2) >= 1)
        self.assertEqual(cands2[0].category, MemoryCategory.TASK)
        self.assertEqual(cands2[0].fact_type, FactType.PLAN)

        # Short noise ignored
        cands3 = await extractor.extract_candidates("hi")
        self.assertEqual(len(cands3), 0)

    # --------------------------------------------------------------------------
    # 3. Duplicate and Near-Duplicate Handling
    # --------------------------------------------------------------------------
    async def test_03_deduplication(self) -> None:
        """Verify exact and near-duplicate detection."""
        detector = DuplicateDetector(near_duplicate_threshold=0.80)
        existing = [
            MemoryModel(
                id="mem-1",
                user_id=1001,
                category="semantic",
                content="User prefers Python for backend development.",
                status="active",
            )
        ]

        # Exact duplicate
        exact_cand = MemoryCandidate(
            category=MemoryCategory.SEMANTIC,
            content="user prefers python for backend development",
        )
        dup = detector.find_duplicate(exact_cand, existing)
        self.assertIsNotNone(dup)
        self.assertEqual(dup.id, "mem-1")

        # Near duplicate (token overlap)
        near_cand = MemoryCandidate(
            category=MemoryCategory.SEMANTIC,
            content="User prefers Python for backend web development.",
        )
        dup_near = detector.find_duplicate(near_cand, existing)
        self.assertIsNotNone(dup_near)

        # Distinct memory
        distinct_cand = MemoryCandidate(
            category=MemoryCategory.SEMANTIC,
            content="User likes playing guitar on weekends.",
        )
        self.assertIsNone(detector.find_duplicate(distinct_cand, existing))

    # --------------------------------------------------------------------------
    # 4. Conflict Detection & Preservation of Established Knowledge
    # --------------------------------------------------------------------------
    async def test_04_conflict_handling(self) -> None:
        """Verify conflicting candidates do not overwrite established facts and are flagged."""
        detector = ConflictDetector()
        existing = [
            MemoryModel(
                id="mem-loc",
                user_id=1001,
                category="semantic",
                content="User lives in London",
                fact_type="explicit_fact",
                confidence=1.0,
                status="active",
            )
        ]

        # Inferred candidate contradicts established fact
        cand = MemoryCandidate(
            category=MemoryCategory.SEMANTIC,
            content="User lives in Tokyo",
            fact_type=FactType.INFERENCE,
            confidence=0.6,
        )

        res = detector.detect_conflict(cand, existing)
        self.assertTrue(res.has_conflict)
        # Must NOT overwrite established explicit fact with an inference!
        self.assertFalse(res.should_overwrite)

        # Ingestion pipeline records conflict into MemoryConflictModel
        pipeline = IngestionPipeline()
        async with self.sessionmaker() as session:
            # Seed established memory
            repo = MemoryRepository(session)
            await repo.create_memory(
                user_id=1001,
                category="semantic",
                content="User lives in London",
                fact_type="explicit_fact",
                confidence=1.0,
            )
            await session.commit()

            # Attempt ingest of conflicting candidate
            mem = await pipeline.ingest_candidate(session, 1001, cand)
            await session.commit()

            self.assertIsNone(mem)  # Rejected from active memories

            # Conflict table must have captured the contradiction
            from sqlalchemy import select
            stmt = select(MemoryConflictModel).where(MemoryConflictModel.user_id == 1001)
            conflicts = (await session.execute(stmt)).scalars().all()
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].status, "unresolved")

    # --------------------------------------------------------------------------
    # 5. Embedding Generation
    # --------------------------------------------------------------------------
    async def test_05_embedding_generation(self) -> None:
        """Verify async and batch embedding generation with dimension tracking."""
        vec = await self.embedding.generate_embedding("Architecture decisions for Qdrant and Neon.")
        self.assertEqual(len(vec), 32)

        batch = await self.embedding.generate_batch(
            ["Sentence 1", "Sentence 2", "Sentence 3"],
            batch_size=2,
        )
        self.assertEqual(len(batch), 3)
        for v in batch:
            self.assertEqual(len(v), 32)

    # --------------------------------------------------------------------------
    # 6. Qdrant Vector Storage & Dense Retrieval
    # --------------------------------------------------------------------------
    async def test_06_qdrant_vector_storage(self) -> None:
        """Verify Qdrant vector upsert, dense search, and payload indexing."""
        import uuid
        mem_id = str(uuid.uuid4())
        vec = await self.embedding.generate_embedding("PostgreSQL relational schema design.")
        await self.qdrant.upsert_vector(
            memory_id=mem_id,
            vector=vec,
            payload={
                "user_id": 1001,
                "category": "project",
                "status": "active",
                "content": "PostgreSQL relational schema design.",
            },
        )

        results = await self.qdrant.search_dense(
            user_id=1001,
            query_vector=vec,
            limit=5,
        )
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0][0], mem_id)
        self.assertAlmostEqual(results[0][1], 1.0, delta=0.01)

    # --------------------------------------------------------------------------
    # 7. PostgreSQL Keyword Retrieval
    # --------------------------------------------------------------------------
    async def test_07_keyword_retrieval(self) -> None:
        """Verify PostgreSQL keyword retrieval with ranking."""
        async with self.sessionmaker() as session:
            repo = MemoryRepository(session)
            await repo.create_memory(
                user_id=1001,
                category="project",
                content="Neon PostgreSQL database migration with Alembic.",
            )
            await repo.create_memory(
                user_id=1001,
                category="project",
                content="Unrelated culinary recipes for dinner.",
            )
            await session.commit()

            retriever = KeywordRetriever()
            candidates = await retriever.retrieve(
                session=session,
                query="Neon PostgreSQL Alembic",
                retrieval_filter=RetrievalFilter(user_id=1001),
                limit=5,
            )
            self.assertTrue(len(candidates) >= 1)
            self.assertIn("Neon PostgreSQL", candidates[0].record.content)

    # --------------------------------------------------------------------------
    # 8. Hybrid Search, Reciprocal Rank Fusion & Reranking
    # --------------------------------------------------------------------------
    async def test_08_hybrid_rrf_and_reranking(self) -> None:
        """Verify Reciprocal Rank Fusion and multi-factor reranking."""
        import uuid
        now = datetime.now(timezone.utc)
        id1 = str(uuid.uuid4())
        id2 = str(uuid.uuid4())

        rec1 = MemoryRecord(
            id=id1,
            user_id=1001,
            category=MemoryCategory.PROJECT,
            content="Qdrant dense vector search implementation.",
            source_reference={},
            importance=0.9,
            confidence=0.95,
            fact_type=FactType.EXPLICIT_FACT,
            project_association="aiagent",
            status=MemoryStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        rec2 = MemoryRecord(
            id=id2,
            user_id=1001,
            category=MemoryCategory.TASK,
            content="Implement hybrid search with RRF.",
            source_reference={},
            importance=0.7,
            confidence=0.9,
            fact_type=FactType.PLAN,
            project_association="aiagent",
            status=MemoryStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )

        c1 = RetrievalCandidate(
            memory_id=id1,
            record=rec1,
            source="dense",
            score=0.95,
            rank=1,
        )
        c2 = RetrievalCandidate(
            memory_id=id2,
            record=rec2,
            source="keyword",
            score=0.80,
            rank=1,
        )

        fuser = ReciprocalRankFusion(k=60)
        fused = fuser.fuse([[c1], [c2]])
        self.assertEqual(len(fused), 2)

        reranker = MemoryReranker()
        reranked = reranker.rerank(fused, target_project="aiagent", top_k=2)
        self.assertEqual(len(reranked), 2)
        self.assertGreater(reranked[0].relevance_score, 0.0)

    # --------------------------------------------------------------------------
    # 9. Strict User-Level Isolation
    # --------------------------------------------------------------------------
    async def test_09_user_isolation(self) -> None:
        """Verify User A can never access or retrieve User B's memories."""
        async with self.sessionmaker() as session:
            repo = MemoryRepository(session)
            # User A memory
            await repo.create_memory(
                user_id=1001,
                category="semantic",
                content="User A secret bank account password.",
            )
            # User B memory
            await repo.create_memory(
                user_id=2002,
                category="semantic",
                content="User B private personal secret.",
            )
            await session.commit()

            # User A keyword search
            kw_retriever = KeywordRetriever()
            res_a = await kw_retriever.retrieve(
                session=session,
                query="secret",
                retrieval_filter=RetrievalFilter(user_id=1001),
            )
            for item in res_a:
                self.assertEqual(item.record.user_id, 1001)
                self.assertNotIn("User B", item.record.content)

            # User B keyword search
            res_b = await kw_retriever.retrieve(
                session=session,
                query="secret",
                retrieval_filter=RetrievalFilter(user_id=2002),
            )
            for item in res_b:
                self.assertEqual(item.record.user_id, 2002)
                self.assertNotIn("User A", item.record.content)

    # --------------------------------------------------------------------------
    # 10. Memory Updates, Invalidation, and Expiration
    # --------------------------------------------------------------------------
    async def test_10_memory_lifecycle(self) -> None:
        """Verify update, invalidation, and expiration of temporary working memories."""
        async with self.sessionmaker() as session:
            repo = MemoryRepository(session)
            # 1. Update content
            mem = await repo.create_memory(
                user_id=1001,
                category="task",
                content="Original task description",
            )
            await session.commit()

            updated = await repo.update_memory(
                memory_id=mem.id,
                user_id=1001,
                content="Updated task description",
                importance=0.9,
            )
            await session.commit()
            self.assertEqual(updated.content, "Updated task description")
            self.assertEqual(updated.importance, 0.9)

            # 2. Invalidation
            invalidated = await repo.set_status(mem.id, 1001, "invalidated")
            await session.commit()
            self.assertTrue(invalidated)
            active_list = await repo.list_active(1001)
            self.assertEqual(len(active_list), 0)

            # 3. Expiration of working memories
            past_time = datetime.now(timezone.utc) - timedelta(hours=1)
            await repo.create_memory(
                user_id=1001,
                category="working",
                content="Temporary working context for code session",
                expires_at=past_time,
            )
            await session.commit()

            expired_count = await repo.expire_old_memories(user_id=1001)
            await session.commit()
            self.assertEqual(expired_count, 1)

    # --------------------------------------------------------------------------
    # 11. Transactional Outbox Synchronization to Qdrant
    # --------------------------------------------------------------------------
    async def test_11_outbox_synchronization(self) -> None:
        """Verify durable outbox synchronization from PostgreSQL to Qdrant."""
        synchronizer = OutboxSynchronizer(self.qdrant, self.embedding)

        async with self.sessionmaker() as session:
            repo = MemoryRepository(session)
            mem = await repo.create_memory(
                user_id=1001,
                category="episodic",
                content="User decided on Neon PostgreSQL and Qdrant stack.",
            )
            await session.commit()

            # Verify outbox entry was queued in PostgreSQL
            outbox_repo = OutboxRepository(session)
            pending = await outbox_repo.fetch_pending_tasks()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].memory_id, mem.id)

            # Process outbox synchronization
            synced_count = await synchronizer.process_batch(session)
            self.assertEqual(synced_count, 1)

            # Task status should now be completed
            pending_after = await outbox_repo.fetch_pending_tasks()
            self.assertEqual(len(pending_after), 0)

        # Verify point exists in Qdrant
        qdrant_results = await self.qdrant.search_dense(
            user_id=1001,
            query_vector=await self.embedding.generate_embedding("Neon Qdrant stack"),
        )
        self.assertTrue(len(qdrant_results) >= 1)
        self.assertEqual(qdrant_results[0][0], mem.id)

    # --------------------------------------------------------------------------
    # 12. Memory Relationships
    # --------------------------------------------------------------------------
    async def test_12_relationships(self) -> None:
        """Verify memory relationship links."""
        async with self.sessionmaker() as session:
            repo = MemoryRepository(session)
            m1 = await repo.create_memory(1001, "project", "Project AIagent")
            m2 = await repo.create_memory(1001, "task", "Implement RAG pipeline")
            await session.commit()

            rel_mgr = RelationshipManager()
            rel = await rel_mgr.link_memories(
                session=session,
                user_id=1001,
                source_memory_id=m1.id,
                target_memory_id=m2.id,
                relationship_type="project_to_task",
            )
            await session.commit()
            self.assertIsNotNone(rel.id)

            rels = await rel_mgr.get_relationships(session, 1001, m1.id)
            self.assertEqual(len(rels), 1)
            self.assertEqual(rels[0].relationship_type, "project_to_task")


if __name__ == "__main__":
    unittest.main()
