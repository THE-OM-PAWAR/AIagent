"""Comprehensive tests for Phase 2: Intelligent Context Engine."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.agent.context.builder import (
    ConstructedContext,
    ContextMemoryItem,
    ContextMessage,
    IntelligentContextBuilder,
)
from src.agent.context.ranking import ContextRanker, ScoredContextItem
from src.agent.context.selector import ContextSelector, SelectionResult
from src.agent.context.summarizer import ConversationSummarizer, SummaryRecord
from src.agent.context.token_budget import (
    MODEL_SPECS,
    ModelSpec,
    TokenBudgetManager,
    TokenEstimator,
    TokenUsageEstimate,
)
from src.database.models import Base, ConversationModel, MessageModel
from src.database.repositories.conversation_repo import ConversationRepository
from src.memory.schemas.memory import FactType, MemoryCategory
from src.memory.schemas.retrieval import RetrievalResult


class TestIntelligentContextEngine(unittest.IsolatedAsyncioTestCase):
    """Test suite covering the 10 completion criteria for the Phase 2 Context Engine."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.sessionmaker = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    # --------------------------------------------------------------------------
    # Criterion 1: Chronological history retrieval
    # --------------------------------------------------------------------------
    async def test_01_chronological_history_retrieval(self) -> None:
        """Verify messages are retrieved in exact chronological order with proper roles and limits."""
        async with self.sessionmaker() as session:
            repo = ConversationRepository(session)
            conv = await repo.get_or_create_active_conversation(user_id=101, conversation_id="conv_test_01")

            # Add turns at spaced intervals
            await repo.add_message("conv_test_01", 101, "user", "Message 1: Hello!")
            await repo.add_message("conv_test_01", 101, "assistant", "Message 2: Hi there!")
            await repo.add_message("conv_test_01", 101, "user", "Message 3: Can you help with Python?")
            await repo.add_message("conv_test_01", 101, "assistant", "Message 4: Yes, of course.")
            await session.commit()

            # Retrieve chronological
            messages = await repo.get_recent_messages("conv_test_01", user_id=101, limit=10, order="asc")
            self.assertEqual(len(messages), 4)
            self.assertEqual(messages[0].content, "Message 1: Hello!")
            self.assertEqual(messages[1].role, "assistant")
            self.assertEqual(messages[2].content, "Message 3: Can you help with Python?")
            self.assertEqual(messages[3].content, "Message 4: Yes, of course.")
            self.assertTrue(messages[0].created_at <= messages[3].created_at)

            # Test limit window (last 2 messages in chronological order)
            limited = await repo.get_recent_messages("conv_test_01", user_id=101, limit=2, order="asc")
            self.assertEqual(len(limited), 2)
            self.assertEqual(limited[0].content, "Message 3: Can you help with Python?")
            self.assertEqual(limited[1].content, "Message 4: Yes, of course.")

    # --------------------------------------------------------------------------
    # Criterion 2: Relevant memory selection across all 4 sources
    # --------------------------------------------------------------------------
    async def test_02_relevant_memory_selection(self) -> None:
        """Verify context selection correctly partitions memories across the 4 sources."""
        selector = ContextSelector()

        retrieved = [
            RetrievalResult(
                memory_id="mem-sem-1",
                category=MemoryCategory.SEMANTIC,
                content="User prefers dark mode and concise code.",
                importance=0.8,
                confidence=1.0,
                relevance_score=0.85,
                source_reference={"test": "source"},
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
            RetrievalResult(
                memory_id="mem-proj-1",
                category=MemoryCategory.PROJECT,
                content="Architecture decision: Use Neon PostgreSQL for persistence.",
                importance=0.9,
                confidence=1.0,
                relevance_score=0.92,
                project_association="ai-agent",
                source_reference={"test": "source"},
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
            RetrievalResult(
                memory_id="mem-task-1",
                category=MemoryCategory.TASK,
                content="Task objective: Complete Phase 2 Context Engine implementation.",
                importance=0.7,
                confidence=0.95,
                relevance_score=0.88,
                project_association="ai-agent",
                source_reference={"test": "source"},
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
        ]

        recent_msgs = [ContextMessage(role="user", content="What database are we using?")]

        res = selector.select_context(
            user_id=202,
            retrieved_results=retrieved,
            recent_messages=recent_msgs,
            active_project="ai-agent",
        )

        self.assertEqual(len(res.selected_items), 3)
        self.assertEqual(len(res.semantic_items), 1)
        self.assertEqual(len(res.project_items), 1)
        self.assertEqual(len(res.task_items), 1)
        self.assertEqual(res.semantic_items[0].item_id, "mem-sem-1")
        self.assertEqual(res.project_items[0].item_id, "mem-proj-1")
        self.assertEqual(res.task_items[0].item_id, "mem-task-1")

    # --------------------------------------------------------------------------
    # Criterion 3: Project and user isolation
    # --------------------------------------------------------------------------
    async def test_03_project_and_user_isolation(self) -> None:
        """Verify strict isolation: cross-user memories are rejected and mismatched project memories excluded."""
        selector = ContextSelector()

        retrieved = [
            # Memory belonging to User 999 (cross-user)
            RetrievalResult(
                memory_id="mem-foreign-user",
                category=MemoryCategory.SEMANTIC,
                content="Secret facts of foreign user.",
                importance=0.9,
                confidence=1.0,
                relevance_score=0.99,
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
                metadata={"user_id": 999},
            ),
            # Memory belonging to another project
            RetrievalResult(
                memory_id="mem-other-project",
                category=MemoryCategory.PROJECT,
                content="Legacy project uses MySQL and Redis.",
                importance=0.8,
                confidence=1.0,
                relevance_score=0.90,
                project_association="other_legacy_project",
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
                metadata={"user_id": 101},
            ),
            # Valid memory matching current user and project
            RetrievalResult(
                memory_id="mem-valid-project",
                category=MemoryCategory.PROJECT,
                content="AI Agent uses Qdrant for vector search.",
                importance=0.85,
                confidence=1.0,
                relevance_score=0.88,
                project_association="ai-agent",
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
                metadata={"user_id": 101},
            ),
        ]

        res = selector.select_context(
            user_id=101,
            retrieved_results=retrieved,
            recent_messages=[],
            active_project="ai-agent",
        )

        self.assertEqual(res.cross_user_rejections, 1)
        self.assertEqual(res.cross_project_rejections, 1)
        self.assertEqual(len(res.selected_items), 1)
        self.assertEqual(res.selected_items[0].item_id, "mem-valid-project")

    # --------------------------------------------------------------------------
    # Criterion 4: Context deduplication
    # --------------------------------------------------------------------------
    async def test_04_context_deduplication(self) -> None:
        """Verify deduplication removes near-duplicate memories and memories already in history."""
        selector = ContextSelector(duplicate_similarity_threshold=0.80)

        retrieved = [
            RetrievalResult(
                memory_id="mem-dup-1",
                category=MemoryCategory.SEMANTIC,
                content="User likes writing asynchronous Python code with type annotations.",
                importance=0.7,
                confidence=1.0,
                relevance_score=0.9,
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
            RetrievalResult(
                memory_id="mem-dup-2",
                category=MemoryCategory.SEMANTIC,
                content="User likes writing asynchronous Python code with type annotations frequently.",
                importance=0.65,
                confidence=1.0,
                relevance_score=0.88,
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
            RetrievalResult(
                memory_id="mem-history-dup",
                category=MemoryCategory.SEMANTIC,
                content="I am working on deploying the telegram bot with long polling.",
                importance=0.5,
                confidence=1.0,
                relevance_score=0.8,
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
        ]

        # Recent message already contains the fact about telegram bot long polling
        recent_messages = [
            ContextMessage(role="user", content="I am working on deploying the telegram bot with long polling."),
            ContextMessage(role="assistant", content="Understood!"),
        ]

        res = selector.select_context(
            user_id=101,
            retrieved_results=retrieved,
            recent_messages=recent_messages,
        )

        # 1 duplicate memory was suppressed, 1 history duplicate was suppressed
        self.assertEqual(res.deduplicated_count, 2)
        self.assertEqual(len(res.selected_items), 1)
        self.assertEqual(res.selected_items[0].item_id, "mem-dup-1")

    # --------------------------------------------------------------------------
    # Criterion 5: Preservation of important decisions
    # --------------------------------------------------------------------------
    async def test_05_preservation_of_important_decisions(self) -> None:
        """Verify critical decisions and negative constraints are protected and prioritized."""
        ranker = ContextRanker()

        decision_item = RetrievalResult(
            memory_id="mem-decision",
            category=MemoryCategory.PROJECT,
            content="We decided that MongoDB will not be used for memory persistence.",
            importance=0.9,
            confidence=1.0,
            relevance_score=0.8,
            source_reference={},
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata={"fact_type": "explicit_fact"},
        )

        constraint_item = RetrievalResult(
            memory_id="mem-constraint",
            category=MemoryCategory.SEMANTIC,
            content="Never expose database ports publicly without TLS encryption.",
            importance=0.95,
            confidence=1.0,
            relevance_score=0.75,
            source_reference={},
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata={"fact_type": "explicit_fact"},
        )

        casual_item = RetrievalResult(
            memory_id="mem-casual",
            category=MemoryCategory.EPISODIC,
            content="User mentioned looking at some weather website yesterday.",
            importance=0.3,
            confidence=0.6,
            relevance_score=0.6,
            source_reference={},
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata={"fact_type": "inference"},
        )

        scored = ranker.rank_items([casual_item, decision_item, constraint_item])

        # Verify decision and constraint were marked as protected
        decision_scored = next(s for s in scored if s.item_id == "mem-decision")
        constraint_scored = next(s for s in scored if s.item_id == "mem-constraint")
        casual_scored = next(s for s in scored if s.item_id == "mem-casual")

        self.assertTrue(decision_scored.is_protected)
        self.assertTrue(decision_scored.is_decision)
        self.assertTrue(constraint_scored.is_protected)
        self.assertTrue(constraint_scored.is_constraint)
        self.assertFalse(casual_scored.is_protected)

        # In ranking, decisions and constraints must score higher than casual notes
        self.assertGreater(decision_scored.final_score, casual_scored.final_score)
        self.assertGreater(constraint_scored.final_score, casual_scored.final_score)

    # --------------------------------------------------------------------------
    # Criterion 6: Token budget enforcement
    # --------------------------------------------------------------------------
    async def test_06_token_budget_enforcement(self) -> None:
        """Verify model-specific budgeting, estimation, and budget pruning protection."""
        mgr = TokenBudgetManager()

        # Check model specifications
        gemini_spec = mgr.get_model_spec("gemini-2.5-flash")
        self.assertEqual(gemini_spec.reserved_response_tokens, 4096)
        self.assertEqual(gemini_spec.default_token_budget, 32768)

        # Test token estimator
        estimator = mgr.estimator
        sample_text = "This is a test of the token estimation subsystem for AIagent context engine."
        token_count = estimator.count_tokens(sample_text)
        self.assertGreater(token_count, 5)
        self.assertLess(token_count, 30)

        # Test builder budget enforcement with severe limit
        builder = IntelligentContextBuilder(default_model="gemini-2.5-flash")

        # Provide diverse non-duplicate memories
        mock_memories = [
            RetrievalResult(
                memory_id="mem-frontend",
                category=MemoryCategory.PROJECT,
                content="Frontend is developed with React, TypeScript, Tailwind CSS, and Vite tooling.",
                importance=0.5,
                confidence=0.8,
                relevance_score=0.7,
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
            RetrievalResult(
                memory_id="mem-backend",
                category=MemoryCategory.PROJECT,
                content="Backend service is written in Python 3.12 with FastAPI and SQLAlchemy async ORM.",
                importance=0.5,
                confidence=0.8,
                relevance_score=0.7,
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
            RetrievalResult(
                memory_id="mem-database",
                category=MemoryCategory.PROJECT,
                content="Primary relational storage runs on Neon Serverless PostgreSQL with SSL encryption.",
                importance=0.5,
                confidence=0.8,
                relevance_score=0.7,
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
            RetrievalResult(
                memory_id="mem-vectors",
                category=MemoryCategory.PROJECT,
                content="Vector storage is powered by Qdrant cloud instance for dense semantic vector search.",
                importance=0.5,
                confidence=0.8,
                relevance_score=0.7,
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
            RetrievalResult(
                memory_id="mem-auth",
                category=MemoryCategory.PROJECT,
                content="User authentication utilizes secure JWT bearer tokens signed with RS256 private keys.",
                importance=0.5,
                confidence=0.8,
                relevance_score=0.7,
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
        ]
        # Include one critical protected decision
        mock_memories.append(
            RetrievalResult(
                memory_id="mem-critical-decision",
                category=MemoryCategory.PROJECT,
                content="Architecture decision: We decided to enforce strict PostgreSQL schemas.",
                importance=0.95,
                confidence=1.0,
                relevance_score=0.9,
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )

        mock_memory_mgr = MagicMock()
        mock_memory_mgr.retrieve = AsyncMock(return_value=mock_memories)
        builder.memory_manager = mock_memory_mgr

        context = await builder.build_context(
            user_id=101,
            query="Test query",
            token_budget=80,  # Tight token budget to trigger pruning
        )

        # Total tokens must not exceed budget
        self.assertLessEqual(context.token_usage.total_used_tokens, 80)
        self.assertGreater(context.token_usage.pruned_items_count, 0)

        # Verify the critical protected decision survived pruning!
        retained_ids = [m.memory_id for m in context.all_memories]
        self.assertIn("mem-critical-decision", retained_ids)

    # --------------------------------------------------------------------------
    # Criterion 7: Conversation summarization
    # --------------------------------------------------------------------------
    async def test_07_conversation_summarization(self) -> None:
        """Verify lightweight conversation summarizer preserves decisions, questions, and tasks."""
        summarizer = ConversationSummarizer(min_messages_to_summarize=4)

        messages = [
            ContextMessage(role="user", content="Let's start the project."),
            ContextMessage(role="assistant", content="Sounds good! What is the goal?"),
            ContextMessage(role="user", content="We decided to use PostgreSQL for data persistence."),
            ContextMessage(role="assistant", content="Great choice. How should we configure SSL certificates?"),
            ContextMessage(role="user", content="Working on task: setup alembic migrations."),
            ContextMessage(role="assistant", content="You must never hardcode secrets in source files."),
        ]

        summary = await summarizer.generate_summary(
            conversation_id="conv_sum_1",
            user_id=101,
            messages=messages,
            cached_summary=None,
        )

        self.assertIsNotNone(summary)
        self.assertIsInstance(summary, SummaryRecord)
        self.assertTrue(summary.is_derived)
        self.assertEqual(summary.version, 1)
        self.assertEqual(summary.message_count, 6)

        # Check preserved elements
        self.assertTrue(any("PostgreSQL" in d for d in summary.decisions))
        self.assertTrue(any("SSL" in q or "certificates" in q for q in summary.unresolved_questions))
        self.assertTrue(any("alembic" in t.lower() for t in summary.task_state))
        self.assertTrue(any("secrets" in c.lower() or "never" in c.lower() for c in summary.constraints_next_steps))

    # --------------------------------------------------------------------------
    # Criterion 8: Summary updates and provenance
    # --------------------------------------------------------------------------
    async def test_08_summary_updates_and_provenance(self) -> None:
        """Verify summary increments version, tracks message range, and skips unnecessary updates."""
        summarizer = ConversationSummarizer(min_messages_to_summarize=4, update_step=4)

        initial_messages = [
            ContextMessage(role="user", content=f"Message {i}", message_id=f"msg_{i}")
            for i in range(5)
        ]

        # Initial summary
        sum_v1 = await summarizer.generate_summary(
            conversation_id="conv_prov_1",
            user_id=101,
            messages=initial_messages,
            cached_summary=None,
        )
        self.assertEqual(sum_v1.version, 1)
        self.assertEqual(sum_v1.start_message_id, "msg_0")
        self.assertEqual(sum_v1.end_message_id, "msg_4")

        # Add only 1 message (less than update_step=4): should NOT trigger update
        extended_1 = list(initial_messages) + [
            ContextMessage(role="user", content="Message 5", message_id="msg_5")
        ]
        sum_cached = await summarizer.generate_summary(
            conversation_id="conv_prov_1",
            user_id=101,
            messages=extended_1,
            cached_summary=sum_v1,
        )
        self.assertEqual(sum_cached.version, 1)
        self.assertEqual(sum_cached.message_count, 5)

        # Add 4 messages (reaches update_step): should update to version 2
        extended_4 = list(initial_messages) + [
            ContextMessage(role="user", content=f"Message {i}", message_id=f"msg_{i}")
            for i in range(5, 10)
        ]
        sum_v2 = await summarizer.generate_summary(
            conversation_id="conv_prov_1",
            user_id=101,
            messages=extended_4,
            cached_summary=sum_v1,
        )
        self.assertEqual(sum_v2.version, 2)
        self.assertEqual(sum_v2.start_message_id, "msg_0")
        self.assertEqual(sum_v2.end_message_id, "msg_9")
        self.assertEqual(sum_v2.message_count, 10)

    # --------------------------------------------------------------------------
    # Criterion 9: Empty history handling
    # --------------------------------------------------------------------------
    async def test_09_empty_history_handling(self) -> None:
        """Verify context builder functions cleanly with empty conversation history."""
        builder = IntelligentContextBuilder()

        # Build context with empty history and no RAG
        context = await builder.build_context(
            user_id=101,
            query="Hello! Who are you?",
            raw_messages=[],
        )

        self.assertIsInstance(context, ConstructedContext)
        self.assertEqual(len(context.messages), 0)
        self.assertEqual(context.query, "Hello! Who are you?")
        self.assertFalse(context.degraded_mode)

        # LLM messages should contain system prompt and user query
        llm_messages = context.to_llm_messages()
        self.assertEqual(len(llm_messages), 2)
        self.assertEqual(llm_messages[0]["role"], "system")
        self.assertEqual(llm_messages[1]["role"], "user")
        self.assertEqual(llm_messages[1]["content"], "Hello! Who are you?")

    # --------------------------------------------------------------------------
    # Criterion 10: Graceful fallback when RAG is unavailable
    # --------------------------------------------------------------------------
    async def test_10_graceful_fallback_when_rag_unavailable(self) -> None:
        """Verify context builder degrades gracefully when RAG retrieval fails."""
        mock_memory_mgr = MagicMock()
        mock_memory_mgr.retrieve = AsyncMock(side_effect=ConnectionError("Qdrant cluster unreachable"))

        builder = IntelligentContextBuilder(memory_manager=mock_memory_mgr)

        history = [
            ContextMessage(role="user", content="What did we discuss earlier?"),
            ContextMessage(role="assistant", content="We discussed Docker containers."),
        ]

        context = await builder.build_context(
            user_id=101,
            query="Tell me more.",
            raw_messages=history,
        )

        # Context must succeed without crashing
        self.assertIsInstance(context, ConstructedContext)
        self.assertTrue(context.degraded_mode)
        self.assertTrue(any("Qdrant" in r or "Memory" in r for r in context.degraded_reasons))
        # History messages must still be preserved
        self.assertEqual(len(context.messages), 2)
        self.assertEqual(context.query, "Tell me more.")

        # Test prompt representation includes degraded status
        prompt_str = context.to_prompt_context_string()
        self.assertIn("Degraded Status: ACTIVE", prompt_str)
        self.assertIn("Tell me more.", prompt_str)


if __name__ == "__main__":
    unittest.main()
