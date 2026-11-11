"""Intelligence Orchestrator coordinating Telegram, PostgreSQL, Memory RAG, Context Engine, and Gemini."""

import asyncio
import logging
import time
from typing import Any

from src.agent.context.builder import ConstructedContext, IntelligentContextBuilder
from src.agent.execution import (
    ConversationLockManager,
    ExecutionRecord,
    ExecutionTracker,
    UpdateDeduplicator,
)
from src.agent.router import AgentRouter, AgentRoutingDecision
from src.agents import (
    AgentResult,
    AgentType,
    AnalystAgent,
    BaseAgent,
    ExecutorAgent,
    GeneralAssistant,
)
from src.database.repositories.conversation_repo import ConversationRepository
from src.database.session import get_async_session
from src.intelligence.providers.gemini import (
    GeminiAPIError,
    GeminiClient,
    GeminiError,
    GeminiRateLimitError,
    GeminiTimeoutError,
)
from src.llm import GeminiProvider, LLMManager, ModelRouter, ModelTier
from src.memory.manager import MemoryManager
from src.tools.executor import ToolExecutor
from src.tools.permissions import PermissionPolicy
from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class OrchestratorError(Exception):
    """Base exception for errors originating in the orchestration layer."""
    pass


class IntelligenceOrchestrator:
    """Central orchestrator managing context generation, intelligence provider calls, and persistence."""

    def __init__(
        self,
        gemini_client: Any,
        database_url: str | None = None,
        memory_manager: MemoryManager | None = None,
        db_client: Any = None,
        context_limit: int = 20,
        system_prompt: str | None = None,
        context_builder: IntelligentContextBuilder | None = None,
        model_name: str | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_executor: ToolExecutor | None = None,
        permission_policy: PermissionPolicy | None = None,
        execution_loop: Any | None = None,
    ) -> None:
        self.gemini_client = gemini_client
        self.database_url = database_url
        self.memory_manager = memory_manager
        self.db_client = db_client
        self.context_limit = max(1, context_limit)
        self.system_prompt = system_prompt or (
            getattr(gemini_client, "_system_instruction", None)
            or "You are a helpful, respectful, and concise personal AI assistant."
        )
        raw_model = model_name or getattr(gemini_client, "model", "gemini-3.6-flash")
        self.model_name = raw_model if isinstance(raw_model, str) else "gemini-3.6-flash"

        # Initialize Context Engine
        self.context_builder = context_builder or IntelligentContextBuilder(
            database_url=self.database_url,
            memory_manager=self.memory_manager,
            default_model=self.model_name,
            system_prompt=self.system_prompt,
        )

        # Reliability and Concurrency Controls
        self.lock_manager = ConversationLockManager()
        self.execution_tracker = ExecutionTracker()
        self.deduplicator = UpdateDeduplicator()

        # Tool System Initialization (empty by default unless tools are explicitly provided)
        self.tool_registry = tool_registry or ToolRegistry()
        self.permission_policy = permission_policy or PermissionPolicy()
        self.tool_executor = tool_executor or ToolExecutor(
            registry=self.tool_registry,
            permission_policy=self.permission_policy,
            execution_tracker=self.execution_tracker,
        )

        # Multi-Agent and Model Routing Infrastructure
        self.llm_provider = GeminiProvider(self.gemini_client)
        self.model_router = ModelRouter(
            small_model="gemini-3.5-flash",
            medium_model=self.model_name,
            large_model=self.model_name,
        )
        self.llm_manager = LLMManager(self.llm_provider, self.model_router)

        # Shared Tool Execution Loop
        if execution_loop is None:
            from src.core.execution_loop import ExecutionLoop
            self.execution_loop = ExecutionLoop(
                tool_registry=self.tool_registry,
                tool_executor=self.tool_executor,
                execution_tracker=self.execution_tracker,
            )
        else:
            self.execution_loop = execution_loop

        self.agent_router = AgentRouter()
        self.agents: dict[AgentType, BaseAgent] = {
            AgentType.GENERAL: GeneralAssistant(self.llm_manager, self.memory_manager, execution_loop=self.execution_loop),
            AgentType.ANALYST: AnalystAgent(self.llm_manager, self.memory_manager, execution_loop=self.execution_loop),
            AgentType.EXECUTOR: ExecutorAgent(self.llm_manager, self.memory_manager, execution_loop=self.execution_loop),
        }

        # Track active background tasks to prevent garbage collection
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def process_message(
        self,
        user_id: int,
        user_text: str,
        update_id: int | None = None,
        conversation_id: str | None = None,
        active_project: str | None = None,
    ) -> str:
        """Process incoming user message through the complete intelligence lifecycle."""
        # 1. Duplicate Telegram update protection
        if update_id is not None and await self.deduplicator.is_duplicate(update_id):
            logger.info("Orchestrator: duplicate update_id=%s rejected for user_id=%s", update_id, user_id)
            return ""

        clean_text = user_text.strip()
        if not clean_text:
            return ""

        # 2. Concurrency control: per-user / per-conversation lock
        lock = await self.lock_manager.acquire(user_id)
        async with lock:
            # 3. Execution tracking initialization
            exec_rec = await self.execution_tracker.start_execution(
                user_id=user_id,
                query=clean_text,
                conversation_id=conversation_id,
                model_name=self.model_name,
            )

            # Masked logging to protect sensitive user content
            logger.info(
                "Orchestrator [%s]: processing turn for user_id=%s (chars=%d)",
                exec_rec.execution_id,
                user_id,
                len(clean_text),
            )

            resolved_conv_id = conversation_id
            active_session = None

            # 4. Resolve active conversation in PostgreSQL or legacy db_client
            if self.database_url:
                try:
                    async with get_async_session(self.database_url) as session:
                        repo = ConversationRepository(session)
                        conv = await repo.get_or_create_active_conversation(
                            user_id=user_id,
                            conversation_id=conversation_id,
                        )
                        resolved_conv_id = conv.conversation_id
                except Exception as exc:
                    logger.error(
                        "Orchestrator [%s]: PostgreSQL conversation lookup failed: %s",
                        exec_rec.execution_id,
                        exc,
                    )
            
            if not resolved_conv_id and self.db_client:
                try:
                    conv = await self.db_client.get_or_create_active_conversation(user_id)
                    resolved_conv_id = conv.get("conversation_id")
                except Exception as exc:
                    logger.error(
                        "Orchestrator [%s]: conversation lookup failed: %s",
                        exec_rec.execution_id,
                        exc,
                    )

            exec_rec.conversation_id = resolved_conv_id

            # 5. Retrieve history & memories, build optimized context via Phase 2 Context Engine
            raw_history_messages = None
            if not self.database_url and self.db_client and resolved_conv_id:
                try:
                    raw_history_messages = await self.db_client.get_recent_messages(
                        conversation_id=resolved_conv_id,
                        limit=self.context_limit,
                    )
                except Exception as exc:
                    logger.warning(
                        "Orchestrator [%s]: history retrieval failed: %s",
                        exec_rec.execution_id,
                        exc,
                    )

            try:
                context: ConstructedContext = await self.context_builder.build_context(
                    user_id=user_id,
                    query=clean_text,
                    conversation_id=resolved_conv_id,
                    active_project=active_project,
                    system_prompt=self.system_prompt,
                    model_name=self.model_name,
                    raw_messages=raw_history_messages,
                    history_limit=self.context_limit,
                )
            except Exception as exc:
                logger.error("Orchestrator [%s]: context construction error: %s", exec_rec.execution_id, exc)
                # Fallback to minimal default context
                context = ConstructedContext(
                    user_id=user_id,
                    query=clean_text,
                    system_prompt=self.system_prompt,
                    token_usage=self.context_builder.budget_manager.estimate_usage(
                        system_prompt=self.system_prompt,
                        messages=[],
                        memories=[],
                        summary=None,
                        reserved_response=1000,
                        max_budget=8000,
                    ),
                    degraded_mode=True,
                    degraded_reasons=[f"Context builder failed: {exc}"],
                )

            # 6. Route to specialized agent
            routing_decision = self.agent_router.route_request(clean_text)
            selected_agent = self.agents.get(routing_decision.selected_agent, self.agents[AgentType.GENERAL])
            logger.info(
                "Orchestrator [%s]: routed to agent '%s' (confidence=%.2f, intent='%s')",
                exec_rec.execution_id,
                selected_agent.agent_type.value,
                routing_decision.confidence,
                routing_decision.intent,
            )

            # 7. Execute specialized agent with timing and error handling
            start_gen = time.perf_counter()
            try:
                agent_result = await selected_agent.execute(
                    context=context,
                    user_text=clean_text,
                    user_id=user_id,
                    project_id=active_project,
                    execution_record=exec_rec,
                )
                response_text = agent_result.response_text
                exec_rec.model_name = agent_result.model_used
            except (GeminiRateLimitError, GeminiTimeoutError, GeminiAPIError, GeminiError) as exc:
                await self.execution_tracker.fail_execution(exec_rec, str(exc))
                raise
            except Exception as exc:
                logger.error(
                    "Orchestrator [%s]: agent '%s' execution error: %s",
                    exec_rec.execution_id,
                    selected_agent.agent_type.value,
                    exc,
                    exc_info=True,
                )
                await self.execution_tracker.fail_execution(exec_rec, str(exc))
                raise OrchestratorError(f"Generation failed: {exc}") from exc

            # 8. Validate model response
            if not response_text or not response_text.strip():
                err_msg = "Model returned empty or whitespace response."
                logger.warning("Orchestrator [%s]: %s", exec_rec.execution_id, err_msg)
                await self.execution_tracker.fail_execution(exec_rec, err_msg)
                raise GeminiAPIError(err_msg)

            clean_response = response_text.strip()
            gen_duration = time.perf_counter() - start_gen

            logger.info(
                "Orchestrator [%s]: response generated (chars=%d, duration=%.2fs, tools_called=%d)",
                exec_rec.execution_id,
                len(clean_response),
                gen_duration,
                len(agent_result.tool_calls),
            )

            # 9. Persist conversation turn in database
            if self.database_url and resolved_conv_id:
                try:
                    async with get_async_session(self.database_url) as session:
                        repo = ConversationRepository(session)
                        await repo.add_message(resolved_conv_id, user_id, "user", clean_text)
                        await repo.add_message(resolved_conv_id, user_id, "model", clean_response)
                except Exception as exc:
                    logger.error(
                        "Orchestrator [%s]: PostgreSQL turn persistence failed: %s",
                        exec_rec.execution_id,
                        exc,
                    )

            if self.db_client and resolved_conv_id:
                try:
                    await self.db_client.save_turn(
                        conversation_id=resolved_conv_id,
                        user_id=user_id,
                        user_message=clean_text,
                        model_response=clean_response,
                    )
                except Exception as exc:
                    logger.error(
                        "Orchestrator [%s]: turn persistence failed: %s",
                        exec_rec.execution_id,
                        exc,
                    )

            # 10. Background memory extraction & update
            if self.memory_manager:
                task = asyncio.create_task(
                    self._safe_background_memory_extraction(
                        user_id=user_id,
                        user_text=clean_text,
                        conversation_id=resolved_conv_id,
                        exec_id=exec_rec.execution_id,
                    )
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

            # 11. Complete execution tracking
            await self.execution_tracker.complete_execution(
                record=exec_rec,
                tokens_estimated=context.token_usage.total_used_tokens,
                degraded_reasons=context.degraded_reasons if context.degraded_mode else None,
            )

            # 12. Return validated response
            return clean_response

    async def _safe_background_memory_extraction(
        self,
        user_id: int,
        user_text: str,
        conversation_id: str | None,
        exec_id: str,
    ) -> None:
        """Execute memory extraction in background with comprehensive exception shielding."""
        if not self.memory_manager:
            return
        try:
            logger.debug("Orchestrator [%s]: triggering background memory extraction...", exec_id)
            await self.memory_manager.remember_turn(
                user_id=user_id,
                user_text=user_text,
                conversation_id=conversation_id,
            )
            logger.debug("Orchestrator [%s]: background memory extraction finished.", exec_id)
        except Exception as exc:
            logger.warning(
                "Orchestrator [%s]: background memory extraction encountered error (non-fatal): %s",
                exec_id,
                exc,
            )

    async def clear_conversation(self, user_id: int) -> bool:
        """Clear active conversation in PostgreSQL and Gemini in-memory state."""
        logger.info("Orchestrator: clearing active conversation for user_id=%s", user_id)
        cleared = False

        if self.database_url:
            try:
                async with get_async_session(self.database_url) as session:
                    repo = ConversationRepository(session)
                    cleared = await repo.clear_active_conversation(user_id)
            except Exception as exc:
                logger.error("Orchestrator: error clearing PostgreSQL conversation for user %s: %s", user_id, exc)

        if self.db_client:
            try:
                legacy_cleared = await self.db_client.clear_active_conversation(user_id)
                cleared = cleared or legacy_cleared
            except Exception as exc:
                logger.error("Orchestrator: error clearing conversation in legacy db_client for user %s: %s", user_id, exc)

        if hasattr(self.gemini_client, "reset_chat"):
            self.gemini_client.reset_chat(user_id)

        return cleared

    async def handle_user_message(
        self,
        user_id: int,
        message_text: str,
        conversation_id: str | None = None,
    ) -> str:
        """Backward-compatible alias for process_message."""
        return await self.process_message(
            user_id=user_id,
            user_text=message_text,
            conversation_id=conversation_id,
        )


# Backward-compatible alias
AgentOrchestrator = IntelligenceOrchestrator
