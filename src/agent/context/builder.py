"""Intelligent Context Builder coordinating history, RAG memories, summarization, and token budgeting."""

from datetime import datetime, timezone
import logging
from typing import Any
from pydantic import BaseModel, Field

from src.agent.context.ranking import ContextRanker, ScoredContextItem
from src.agent.context.selector import ContextSelector, SelectionResult
from src.agent.context.summarizer import ConversationSummarizer, SummaryRecord
from src.agent.context.token_budget import TokenBudgetManager, TokenUsageEstimate
from src.database.repositories.conversation_repo import ConversationRepository
from src.database.session import get_async_session
from src.memory.manager import MemoryManager
from src.memory.schemas.retrieval import RetrievalResult

logger = logging.getLogger(__name__)


class ContextMessage(BaseModel):
    """Normalized message representation for context construction."""
    role: str
    content: str
    message_id: str | None = None
    created_at: str | None = None


class ContextMemoryItem(BaseModel):
    """Context-ready memory representation with traceability and importance attributes."""
    memory_id: str
    category: str
    content: str
    importance: float
    confidence: float
    relevance_score: float
    project_association: str | None = None
    is_protected: bool = False
    is_decision: bool = False
    is_constraint: bool = False
    source_reference: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConstructedContext(BaseModel):
    """Complete structured context package ready for downstream model consumption."""
    user_id: int
    query: str
    project_id: str | None = None
    system_prompt: str = ""
    summary: SummaryRecord | None = None
    messages: list[ContextMessage] = Field(default_factory=list)
    semantic_memories: list[ContextMemoryItem] = Field(default_factory=list)
    project_memories: list[ContextMemoryItem] = Field(default_factory=list)
    task_memories: list[ContextMemoryItem] = Field(default_factory=list)
    all_memories: list[ContextMemoryItem] = Field(default_factory=list)
    token_usage: TokenUsageEstimate
    selection_metadata: dict[str, Any] = Field(default_factory=dict)
    source_references: list[dict[str, Any]] = Field(default_factory=list)
    degraded_mode: bool = False
    degraded_reasons: list[str] = Field(default_factory=list)

    def to_llm_messages(self, model_family: str = "standard") -> list[dict[str, str]]:
        """Format the structured context into provider-agnostic message turns.

        Returns a list of dictionaries with 'role' ('system', 'user', 'model'/'assistant')
        and 'content', suitable for Gemini, OpenAI, Anthropic, or local LLMs.
        """
        system_sections = []
        if self.system_prompt:
            system_sections.append(self.system_prompt.strip())

        # Memory Sections
        if self.semantic_memories:
            facts = "\n".join(f"- {m.content}" for m in self.semantic_memories)
            system_sections.append(f"[USER FACTS & PREFERENCES]\n{facts}")

        if self.project_memories:
            proj_facts = "\n".join(f"- {m.content}" for m in self.project_memories)
            proj_title = f"[PROJECT CONTEXT: {self.project_id}]" if self.project_id else "[PROJECT CONTEXT & DECISIONS]"
            system_sections.append(f"{proj_title}\n{proj_facts}")

        if self.task_memories:
            tasks = "\n".join(f"- {m.content}" for m in self.task_memories)
            system_sections.append(f"[ACTIVE TASKS & WORKING STATE]\n{tasks}")

        if self.summary and self.summary.content:
            system_sections.append(f"[CONVERSATION SUMMARY]\n{self.summary.content.strip()}")

        combined_system = "\n\n".join(system_sections)

        turns: list[dict[str, str]] = []

        # System message (or prepended to first turn depending on target)
        if combined_system:
            role_tag = "system" if model_family != "gemini" else "system"
            turns.append({"role": role_tag, "content": combined_system})

        # History turns (chronological)
        for msg in self.messages:
            role = "model" if msg.role in {"assistant", "bot", "model"} else "user"
            turns.append({"role": role, "content": msg.content})

        # Query turn
        if self.query:
            turns.append({"role": "user", "content": self.query})

        return turns

    def to_prompt_context_string(self) -> str:
        """Render a readable text representation of all included context blocks."""
        sections = []
        sections.append("=== CONTEXT PACKAGE ===")
        sections.append(f"User ID: {self.user_id} | Project: {self.project_id or 'None'}")
        sections.append(f"Token Budget: {self.token_usage.total_used_tokens} / {self.token_usage.max_budget} tokens")

        if self.degraded_mode:
            sections.append(f"Degraded Status: ACTIVE ({', '.join(self.degraded_reasons)})")

        if self.summary:
            sections.append(f"\n--- Conversation Summary (v{self.summary.version}) ---")
            sections.append(self.summary.content)

        if self.all_memories:
            sections.append(f"\n--- Retrieved Memories ({len(self.all_memories)}) ---")
            for m in self.all_memories:
                protected = " [PROTECTED]" if m.is_protected else ""
                sections.append(f"[{m.category.upper()} | imp={m.importance:.2f} | rel={m.relevance_score:.2f}]{protected} {m.content}")

        if self.messages:
            sections.append(f"\n--- Recent History ({len(self.messages)} turns) ---")
            for msg in self.messages:
                sections.append(f"{msg.role.upper()}: {msg.content}")

        sections.append(f"\n--- Current Request ---\nUSER: {self.query}")
        return "\n".join(sections)


class IntelligentContextBuilder:
    """Production-grade context construction engine with hybrid RAG, ranking, and token budgeting."""

    def __init__(
        self,
        database_url: str | None = None,
        memory_manager: MemoryManager | None = None,
        default_model: str = "gemini-2.5-flash",
        system_prompt: str = "You are a helpful, respectful, and concise personal AI assistant.",
    ) -> None:
        self.database_url = database_url
        self.memory_manager = memory_manager
        self.default_model = default_model
        self.system_prompt = system_prompt

        self.ranker = ContextRanker()
        self.selector = ContextSelector(ranker=self.ranker)
        self.budget_manager = TokenBudgetManager(default_model=default_model)
        self.summarizer = ConversationSummarizer()

    async def build_context(
        self,
        user_id: int,
        query: str,
        conversation_id: str | None = None,
        active_project: str | None = None,
        system_prompt: str | None = None,
        model_name: str | None = None,
        token_budget: int | None = None,
        raw_messages: list[Any] | None = None,
        history_limit: int = 20,
    ) -> ConstructedContext:
        """Construct an optimized, budgeted, and isolated context object for a user request."""
        clean_query = query.strip()
        effective_system_prompt = (system_prompt or self.system_prompt).strip()
        effective_model = model_name or self.default_model

        degraded = False
        degraded_reasons: list[str] = []

        # 1. Fetch Chronological Conversation History
        chronological_messages: list[ContextMessage] = []
        cached_summary: dict[str, Any] | None = None

        if raw_messages is not None:
            # Use provided messages (e.g. from MongoDB fallback or direct test inputs)
            for m in raw_messages[-history_limit:]:
                role = str(m.get("role", "user") if isinstance(m, dict) else getattr(m, "role", "user"))
                content = str(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", ""))
                msg_id = str(m.get("id") or m.get("message_id") if isinstance(m, dict) else getattr(m, "id", None))
                created = str(m.get("created_at") if isinstance(m, dict) else getattr(m, "created_at", None))
                if content:
                    chronological_messages.append(
                        ContextMessage(role=role, content=content, message_id=msg_id, created_at=created)
                    )
        elif self.database_url and conversation_id:
            try:
                async with get_async_session(self.database_url) as session:
                    repo = ConversationRepository(session)
                    db_messages = await repo.get_recent_messages(
                        conversation_id=conversation_id,
                        user_id=user_id,
                        limit=history_limit,
                        order="asc",
                    )
                    cached_summary = await repo.get_summary(conversation_id=conversation_id)

                    for m in db_messages:
                        chronological_messages.append(
                            ContextMessage(
                                role=m.role,
                                content=m.content,
                                message_id=m.id,
                                created_at=m.created_at.isoformat() if m.created_at else None,
                            )
                        )
            except Exception as exc:
                degraded = True
                degraded_reasons.append(f"PostgreSQL history retrieval unavailable: {exc}")
                logger.error("Failed to retrieve conversation history from PostgreSQL: %s", exc)

        # 2. Hybrid RAG Memory Retrieval via Phase 1
        retrieved_memories: list[RetrievalResult] = []
        if self.memory_manager:
            try:
                retrieved_memories = await self.memory_manager.retrieve(
                    user_id=user_id,
                    query=clean_query,
                    project_association=active_project,
                    top_k=10,
                )
            except Exception as exc:
                degraded = True
                degraded_reasons.append(f"Memory RAG retrieval unavailable: {exc}")
                logger.error("MemoryManager retrieval error for user_id=%d: %s", user_id, exc)

        # 3. Context Selection, Deduplication & Isolation
        selection_res: SelectionResult = self.selector.select_context(
            user_id=user_id,
            retrieved_results=retrieved_memories,
            recent_messages=chronological_messages,
            active_project=active_project,
        )

        # 4. Conversation Summarization (if message volume exceeds threshold)
        active_conv_id = conversation_id or "conv_default"
        summary_record: SummaryRecord | None = None
        if len(chronological_messages) >= self.summarizer.min_messages_to_summarize:
            try:
                summary_record = await self.summarizer.generate_summary(
                    conversation_id=active_conv_id,
                    user_id=user_id,
                    messages=chronological_messages,
                    cached_summary=cached_summary,
                )
                if summary_record and self.database_url and conversation_id:
                    # Persist summary cache asynchronously in PostgreSQL
                    try:
                        async with get_async_session(self.database_url) as session:
                            repo = ConversationRepository(session)
                            await repo.update_summary(conversation_id, summary_record.model_dump())
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning("Summarization skipped due to error: %s", exc)

        # 5. Token Budgeting & Progressive Pruning
        usable_budget, reserved_response = self.budget_manager.calculate_effective_budget(
            model_name=effective_model,
            max_budget=token_budget,
        )

        # Prepare candidate memory items
        candidate_memories = list(selection_res.selected_items)
        candidate_messages = list(chronological_messages)
        pruned_count = 0

        # Calculate initial token usage
        est = self.budget_manager.estimate_usage(
            system_prompt=effective_system_prompt,
            messages=candidate_messages,
            memories=candidate_memories,
            summary=summary_record.content if summary_record else None,
            reserved_response=reserved_response,
            max_budget=token_budget or self.budget_manager.get_model_spec(effective_model).default_token_budget,
        )

        # Budget enforcement: If over usable budget, prune lowest-ranked non-protected memories first,
        # then oldest history turns
        while est.total_used_tokens > est.max_budget:
            # 1. Try pruning unprotected memories from the end of the ranked list
            unprotected_idx = None
            for idx in reversed(range(len(candidate_memories))):
                if not candidate_memories[idx].is_protected:
                    unprotected_idx = idx
                    break

            if unprotected_idx is not None:
                pruned_item = candidate_memories.pop(unprotected_idx)
                pruned_count += 1
                logger.debug("Budget exceeded: pruned memory '%s...'", pruned_item.content[:30])
            elif len(candidate_messages) > 2:
                # 2. Prune oldest history turn (preserving newest context)
                pruned_msg = candidate_messages.pop(0)
                pruned_count += 1
                logger.debug("Budget exceeded: pruned historical message '%s...'", pruned_msg.content[:30])
            elif candidate_memories:
                # 3. Only if absolutely necessary, prune lowest protected memory
                pruned_item = candidate_memories.pop()
                pruned_count += 1
            else:
                # Reached irreducible minimum
                break

            # Recompute usage
            est = self.budget_manager.estimate_usage(
                system_prompt=effective_system_prompt,
                messages=candidate_messages,
                memories=candidate_memories,
                summary=summary_record.content if summary_record else None,
                reserved_response=reserved_response,
                max_budget=token_budget or self.budget_manager.get_model_spec(effective_model).default_token_budget,
            )

        est.pruned_items_count = pruned_count

        # Convert final memories into ContextMemoryItem
        semantic_memories: list[ContextMemoryItem] = []
        project_memories: list[ContextMemoryItem] = []
        task_memories: list[ContextMemoryItem] = []
        all_memories: list[ContextMemoryItem] = []
        source_refs: list[dict[str, Any]] = []

        for item in candidate_memories:
            c_mem = ContextMemoryItem(
                memory_id=item.item_id,
                category=item.category,
                content=item.content,
                importance=item.importance,
                confidence=item.confidence,
                relevance_score=item.relevance_score,
                project_association=item.original_item.project_association if hasattr(item.original_item, "project_association") else None,
                is_protected=item.is_protected,
                is_decision=item.is_decision,
                is_constraint=item.is_constraint,
                source_reference=item.source_reference,
                metadata=item.metadata,
            )
            all_memories.append(c_mem)
            if item.source_reference:
                source_refs.append(item.source_reference)

            cat = item.category.lower()
            if cat in {"semantic", "episodic"}:
                semantic_memories.append(c_mem)
            elif cat == "project":
                project_memories.append(c_mem)
            elif cat in {"task", "working"}:
                task_memories.append(c_mem)
            else:
                semantic_memories.append(c_mem)

        # Add history message references
        for m in candidate_messages:
            if m.message_id:
                source_refs.append({"message_id": m.message_id, "role": m.role})

        metadata = {
            "model_name": effective_model,
            "deduplicated_memories_count": selection_res.deduplicated_count,
            "cross_user_rejections": selection_res.cross_user_rejections,
            "cross_project_rejections": selection_res.cross_project_rejections,
            "pruned_items_count": pruned_count,
            "explanations": selection_res.explanations,
        }

        return ConstructedContext(
            user_id=user_id,
            query=clean_query,
            project_id=active_project,
            system_prompt=effective_system_prompt,
            summary=summary_record,
            messages=candidate_messages,
            semantic_memories=semantic_memories,
            project_memories=project_memories,
            task_memories=task_memories,
            all_memories=all_memories,
            token_usage=est,
            selection_metadata=metadata,
            source_references=source_refs,
            degraded_mode=degraded,
            degraded_reasons=degraded_reasons,
        )

