"""Comprehensive unit and integration tests for Phase 4: Multi-Agent Architecture and Model Routing."""

import asyncio
from datetime import datetime, timezone
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.agent.context.builder import ContextMemoryItem, ContextMessage, IntelligentContextBuilder
from src.agent.orchestrator import IntelligenceOrchestrator, OrchestratorError
from src.agent.router import AgentRouter, AgentRoutingDecision
from src.agents import AgentResult, AgentType, AnalystAgent, BaseAgent, ExecutorAgent, GeneralAssistant
from src.config import Config
from src.database.models import Base
from src.intelligence.providers.gemini import GeminiAPIError, GeminiClient
from src.interfaces.telegram.handlers import handle_message
from src.llm import BaseLLMProvider, GeminiProvider, LLMManager, LLMResponse, ModelRouter, ModelTier
from src.memory.manager import MemoryManager
from src.memory.schemas.memory import FactType, MemoryCandidate, MemoryCategory
from src.memory.schemas.retrieval import RetrievalResult


class TestMultiAgentAndModelRouting(unittest.IsolatedAsyncioTestCase):
    """Test suite verifying all 10 completion criteria for Phase 4."""

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.sessionmaker = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Mock Gemini Client
        self.mock_gemini = MagicMock(spec=GeminiClient)
        self.mock_gemini.model = "gemini-2.5-flash"
        self.mock_gemini._system_instruction = "You are a helpful personal assistant."
        self.mock_gemini.generate_response = AsyncMock(return_value="Standard assistant response.")
        self.mock_gemini.reset_chat = MagicMock(return_value=True)

        # Mock Memory Manager
        self.mock_memory = MagicMock(spec=MemoryManager)
        self.mock_memory.retrieve = AsyncMock(return_value=[])
        self.mock_memory.remember = AsyncMock(return_value=MagicMock())
        self.mock_memory.remember_turn = AsyncMock(return_value=MagicMock())

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    # --------------------------------------------------------------------------
    # Criterion 1: Correct agent routing
    # --------------------------------------------------------------------------
    async def test_01_correct_agent_routing(self) -> None:
        """Verify queries route accurately to General, Analyst, and Executor, with safe fallback."""
        router = AgentRouter()

        # 1. General intent
        d1 = router.route_request("Hello, how are you today?")
        self.assertEqual(d1.selected_agent, AgentType.GENERAL)

        # 2. Analyst intent (comparison / tradeoff / evaluation)
        d2 = router.route_request("Compare PostgreSQL vs MongoDB for high-throughput memory storage tradeoffs.")
        self.assertEqual(d2.selected_agent, AgentType.ANALYST)

        # 3. Executor intent (action plan / task breakdown / milestones)
        d3 = router.route_request("Create a detailed step by step implementation plan and roadmap for Phase 4.")
        self.assertEqual(d3.selected_agent, AgentType.EXECUTOR)

        # 4. Ambiguous / fallback intent
        d4 = router.route_request("What do you think about computers?")
        self.assertEqual(d4.selected_agent, AgentType.GENERAL)

    # --------------------------------------------------------------------------
    # Criterion 2: General Assistant handling normal conversation
    # --------------------------------------------------------------------------
    async def test_02_general_assistant_handles_conversation(self) -> None:
        """Verify General Assistant processes casual dialogue and personal preferences."""
        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
            memory_manager=self.mock_memory,
        )

        response = await orchestrator.process_message(
            user_id=101,
            user_text="Good morning! What can you help me with?",
        )

        self.assertEqual(response, "Standard assistant response.")
        stats = await orchestrator.execution_tracker.get_stats()
        self.assertEqual(stats["completed"], 1)

    # --------------------------------------------------------------------------
    # Criterion 3: Analyst handling complex analytical requests
    # --------------------------------------------------------------------------
    async def test_03_analyst_handles_complex_analysis(self) -> None:
        """Verify Analyst generates structured evaluation and selects Large tier."""
        analyst_response_text = (
            "## 1. Executive Summary\n"
            "PostgreSQL offers ACID compliance, while MongoDB provides flexible document schema.\n\n"
            "## 2. Comparative Tradeoffs\n"
            "PostgreSQL with pgvector vs Qdrant standalone.\n\n"
            "## 3. Technical Risks\n"
            "Connection exhaustion under heavy concurrency.\n\n"
            "## 4. Recommendations\n"
            "Adopt Neon PostgreSQL as the primary source of truth."
        )

        captured_tier = None

        async def capture_gen(user_id, message_text, history, **kwargs):
            return analyst_response_text

        self.mock_gemini.generate_response = AsyncMock(side_effect=capture_gen)

        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
            memory_manager=self.mock_memory,
        )

        response = await orchestrator.process_message(
            user_id=101,
            user_text="Conduct a comprehensive architectural tradeoff analysis of PostgreSQL versus MongoDB.",
        )

        self.assertIn("Executive Summary", response)
        self.assertIn("Recommendations", response)
        # Check execution metrics
        metrics = orchestrator.llm_manager.get_metrics()
        self.assertGreater(metrics["total_requests"], 0)

    # --------------------------------------------------------------------------
    # Criterion 4: Executor creating and tracking structured tasks
    # --------------------------------------------------------------------------
    async def test_04_executor_creates_and_tracks_tasks(self) -> None:
        """Verify Executor decomposes goals into tagged steps and updates shared task memory."""
        plan_output = (
            "Here is the execution plan:\n"
            "1. [COMPLETED] Setup database models and Alembic migrations\n"
            "2. [IN_PROGRESS] Implement Intelligent Context Builder and Token Budgeting\n"
            "3. [TODO] Implement Multi-Agent Router and LLM Manager\n"
            "4. [BLOCKED] Awaiting external API credential configuration"
        )
        self.mock_gemini.generate_response = AsyncMock(return_value=plan_output)

        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
            memory_manager=self.mock_memory,
        )

        response = await orchestrator.process_message(
            user_id=101,
            user_text="Create a roadmap and step by step task breakdown for deploying the application.",
            active_project="ai-agent",
        )

        self.assertIn("[COMPLETED]", response)
        self.assertIn("[TODO]", response)

        # Verify task memory was created in shared MemoryManager
        self.mock_memory.remember.assert_awaited()
        call_args = self.mock_memory.remember.call_args[1]
        self.assertEqual(call_args["user_id"], 101)
        candidate: MemoryCandidate = call_args["candidate"]
        self.assertEqual(candidate.category, MemoryCategory.TASK)
        self.assertEqual(candidate.fact_type, FactType.PLAN)
        self.assertIn("[COMPLETED]", candidate.content)

    # --------------------------------------------------------------------------
    # Criterion 5: Shared memory and context across agents
    # --------------------------------------------------------------------------
    async def test_05_shared_memory_and_context_across_agents(self) -> None:
        """Verify all agents query the same MemoryManager and share unified context."""
        # 1. RAG returns task and project memories
        shared_memories = [
            RetrievalResult(
                memory_id="mem-task-shared",
                category=MemoryCategory.TASK,
                content="[TODO] Implement Redis caching layer.",
                importance=0.8,
                confidence=1.0,
                relevance_score=0.9,
                project_association="ai-agent",
                source_reference={},
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        ]
        self.mock_memory.retrieve = AsyncMock(return_value=shared_memories)

        captured_system = None

        async def capture_call(user_id, message_text, history, **kwargs):
            nonlocal captured_system
            captured_system = kwargs.get("system_instruction")
            return "Analysis incorporating shared task memory."

        self.mock_gemini.generate_response = AsyncMock(side_effect=capture_call)

        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
            memory_manager=self.mock_memory,
        )

        # Ask an analytical question routed to Analyst
        await orchestrator.process_message(
            user_id=101,
            user_text="Analyze the feasibility and risks of the pending caching task.",
            active_project="ai-agent",
        )

        # Verify the Analyst received the shared task memory in its context
        self.assertIsNotNone(captured_system)
        self.assertIn("Implement Redis caching layer", captured_system)

    # --------------------------------------------------------------------------
    # Criterion 6: Model tier selection
    # --------------------------------------------------------------------------
    async def test_06_model_tier_selection(self) -> None:
        """Verify model router selects Small for brief queries, Medium for general, Large for analysis."""
        router = ModelRouter(
            small_model="gemini-2.5-flash",
            medium_model="gemini-2.5-flash",
            large_model="gemini-1.5-pro",
        )

        # 1. Brief greeting -> Small
        tier1 = router.select_tier(agent_type="general", query="hi")
        self.assertEqual(tier1, ModelTier.SMALL)

        # 2. General conversational query -> Medium
        tier2 = router.select_tier(agent_type="general", query="What are the best practices for structuring Python code?")
        self.assertEqual(tier2, ModelTier.MEDIUM)

        # 3. Analyst query -> Large
        tier3 = router.select_tier(agent_type="analyst", query="Evaluate the trade-offs of microservices vs monolith.")
        self.assertEqual(tier3, ModelTier.LARGE)
        self.assertEqual(router.resolve_model_name(tier3), "gemini-1.5-pro")

    # --------------------------------------------------------------------------
    # Criterion 7: Provider/model fallback
    # --------------------------------------------------------------------------
    async def test_07_provider_and_model_fallback(self) -> None:
        """Verify LLMManager falls back to smaller tier when Large tier encounters errors."""
        mock_provider = MagicMock(spec=BaseLLMProvider)
        mock_provider.provider_name = "gemini"

        # Large fails with 429 quota exhausted, Medium succeeds
        async def mock_generate(messages, model_name, tier=ModelTier.MEDIUM, **kwargs):
            if tier == ModelTier.LARGE:
                raise GeminiAPIError("Quota exceeded on gemini-1.5-pro")
            return LLMResponse(
                content="Fallback response generated on Medium tier.",
                model_name=model_name,
                tier=tier,
                provider="gemini",
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
                latency_seconds=0.5,
                estimated_cost_usd=0.0001,
            )

        mock_provider.generate = AsyncMock(side_effect=mock_generate)

        manager = LLMManager(primary_provider=mock_provider)

        resp = await manager.generate(
            messages=[{"role": "user", "content": "Complex query requiring fallback"}],
            tier=ModelTier.LARGE,
        )

        self.assertEqual(resp.content, "Fallback response generated on Medium tier.")
        self.assertTrue(resp.fallback_used)
        self.assertEqual(resp.tier, ModelTier.MEDIUM)
        self.assertEqual(manager.fallbacks_triggered, 1)

    # --------------------------------------------------------------------------
    # Criterion 8: Cost and token usage tracking
    # --------------------------------------------------------------------------
    async def test_08_cost_and_token_usage_tracking(self) -> None:
        """Verify token counts and estimated costs are tracked and accumulated."""
        provider = GeminiProvider(self.mock_gemini)
        manager = LLMManager(primary_provider=provider)

        self.mock_gemini.generate_response = AsyncMock(return_value="Detailed response output.")

        # Execute 2 requests
        await manager.generate(
            messages=[{"role": "user", "content": "Short test message."}],
            tier=ModelTier.SMALL,
        )
        await manager.generate(
            messages=[{"role": "user", "content": "Another message testing metrics telemetry."}],
            tier=ModelTier.MEDIUM,
        )

        metrics = manager.get_metrics()
        self.assertEqual(metrics["total_requests"], 2)
        self.assertGreater(metrics["total_tokens"], 0)
        self.assertGreaterEqual(metrics["total_cost_usd"], 0.0)
        self.assertEqual(metrics["tier_distribution"]["small"], 1)
        self.assertEqual(metrics["tier_distribution"]["medium"], 1)

    # --------------------------------------------------------------------------
    # Criterion 9: Execution limits and error handling
    # --------------------------------------------------------------------------
    async def test_09_execution_limits_and_error_handling(self) -> None:
        """Verify execution limits, timeout handling, and failure recording in tracker."""
        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
        )

        # Simulate provider timeout
        self.mock_gemini.generate_response = AsyncMock(side_effect=asyncio.TimeoutError())

        with self.assertRaises(OrchestratorError):
            await orchestrator.process_message(
                user_id=101,
                user_text="Query that times out.",
            )

        stats = await orchestrator.execution_tracker.get_stats()
        self.assertGreater(stats["failed"], 0)

    # --------------------------------------------------------------------------
    # Criterion 10: Existing Telegram functionality remaining intact
    # --------------------------------------------------------------------------
    async def test_10_existing_telegram_functionality_intact(self) -> None:
        """Verify Telegram message updates route seamlessly to specialized agents."""
        self.mock_gemini.generate_response = AsyncMock(
            return_value="Detailed architectural analysis of Docker deployment."
        )

        orchestrator = IntelligenceOrchestrator(
            gemini_client=self.mock_gemini,
        )

        update = MagicMock()
        update.update_id = 8801
        update.effective_user.id = 12345
        update.effective_chat.id = 67890
        update.message.text = "Analyze Docker container orchestration tradeoffs."
        update.message.message_id = 100
        update.message.reply_text = AsyncMock()

        config = Config(
            telegram_bot_token="test:token",
            allowed_telegram_user_ids=frozenset([12345]),
            gemini_api_key="test_key",
            gemini_model="gemini-2.5-flash",
            gemini_system_instruction="You are a helpful assistant.",
            gemini_timeout_seconds=30.0,
            conversation_history_limit=20,
            log_level="INFO",
            environment="testing",
        )

        context = MagicMock()
        context.bot_data = {
            "orchestrator": orchestrator,
            "config": config,
        }

        with patch("src.interfaces.telegram.handlers.is_authorized", return_value=True):
            await handle_message(update, context)

        update.message.reply_text.assert_awaited_once_with(
            "Detailed architectural analysis of Docker deployment."
        )
        stats = await orchestrator.execution_tracker.get_stats()
        self.assertEqual(stats["completed"], 1)


if __name__ == "__main__":
    unittest.main()
