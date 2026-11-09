"""Lightweight conversation summarization preserving decisions, tasks, and questions."""

from datetime import datetime, timezone
import logging
import re
from typing import Any
import uuid
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Heuristic patterns for extractive fallback summarizer
_DECISION_PATTERNS = re.compile(
    r"\b(decided (?:to|that)|agreed (?:to|on)|chosen|selected|architecture choice|will use)\b.*",
    re.IGNORECASE,
)
_QUESTION_PATTERNS = re.compile(
    r"(\b(?:how|what|why|when|where|should we|can we|do we|is there)\b.*\?|\b(?:todo|unresolved|pending|question)\b.*)",
    re.IGNORECASE,
)
_TASK_PATTERNS = re.compile(
    r"\b(task|next step|working on|objective|progress|status|phase \d)\b.*",
    re.IGNORECASE,
)
_CONSTRAINT_PATTERNS = re.compile(
    r"\b(must|never|always|do not|cannot|restriction|constraint|requirement)\b.*",
    re.IGNORECASE,
)


class SummaryRecord(BaseModel):
    """Structured representation of a derived conversation summary."""
    summary_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str
    user_id: int
    version: int = 1
    start_message_id: str | None = None
    end_message_id: str | None = None
    message_count: int = 0
    content: str
    main_discussion: str = ""
    decisions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    task_state: list[str] = Field(default_factory=list)
    constraints_next_steps: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    is_derived: bool = True  # Explicit indicator that this is derived, not authoritative


class ConversationSummarizer:
    """Produces and maintains versioned summaries of long conversation threads."""

    def __init__(
        self,
        gemini_client: Any = None,
        min_messages_to_summarize: int = 6,
        update_step: int = 4,
    ) -> None:
        self.gemini_client = gemini_client
        self.min_messages_to_summarize = min_messages_to_summarize
        self.update_step = update_step

    def should_update_summary(
        self,
        current_message_count: int,
        cached_summary: SummaryRecord | dict[str, Any] | None,
    ) -> bool:
        """Determine if an update is warranted to avoid unnecessary processing."""
        if current_message_count < self.min_messages_to_summarize:
            return False

        if cached_summary is None:
            return True

        cached_count = (
            cached_summary.message_count
            if isinstance(cached_summary, SummaryRecord)
            else cached_summary.get("message_count", 0)
        )
        return (current_message_count - cached_count) >= self.update_step

    def extract_structured_summary_heuristics(
        self,
        messages: list[Any],
        conversation_id: str,
        user_id: int,
        version: int = 1,
    ) -> SummaryRecord:
        """Deterministic extractive summarization for tests and offline resilience."""
        decisions: list[str] = []
        questions: list[str] = []
        tasks: list[str] = []
        constraints: list[str] = []
        key_points: list[str] = []

        start_id = None
        end_id = None

        for idx, msg in enumerate(messages):
            msg_id = getattr(msg, "id", None) or getattr(msg, "message_id", str(idx))
            if idx == 0:
                start_id = str(msg_id)
            end_id = str(msg_id)

            content = getattr(msg, "content", "") if not isinstance(msg, dict) else msg.get("content", "")
            role = getattr(msg, "role", "user") if not isinstance(msg, dict) else msg.get("role", "user")

            for line in content.splitlines():
                line_str = line.strip()
                if not line_str:
                    continue

                if _DECISION_PATTERNS.search(line_str):
                    decisions.append(line_str[:120])
                if _QUESTION_PATTERNS.search(line_str):
                    questions.append(line_str[:120])
                if _TASK_PATTERNS.search(line_str):
                    tasks.append(line_str[:120])
                if _CONSTRAINT_PATTERNS.search(line_str):
                    constraints.append(line_str[:120])

            if idx % 3 == 0 and content:
                snippet = content[:80] + ("..." if len(content) > 80 else "")
                key_points.append(f"{role.capitalize()}: {snippet}")

        # Assemble formatted summary text
        summary_lines = []
        main_disc = "; ".join(key_points[:5]) if key_points else "General ongoing discussion."
        summary_lines.append(f"Main Discussion: {main_disc}")

        if decisions:
            summary_lines.append("Key Decisions: " + " | ".join(decisions[:4]))
        if questions:
            summary_lines.append("Open Questions: " + " | ".join(questions[:4]))
        if tasks:
            summary_lines.append("Task State: " + " | ".join(tasks[:4]))
        if constraints:
            summary_lines.append("Constraints & Next Steps: " + " | ".join(constraints[:4]))

        full_content = "\n".join(summary_lines)

        return SummaryRecord(
            conversation_id=conversation_id,
            user_id=user_id,
            version=version,
            start_message_id=start_id,
            end_message_id=end_id,
            message_count=len(messages),
            content=full_content,
            main_discussion=main_disc,
            decisions=decisions[:5],
            unresolved_questions=questions[:5],
            task_state=tasks[:5],
            constraints_next_steps=constraints[:5],
        )

    async def generate_summary(
        self,
        conversation_id: str,
        user_id: int,
        messages: list[Any],
        cached_summary: SummaryRecord | dict[str, Any] | None = None,
    ) -> SummaryRecord | None:
        """Produce or update conversation summary without altering underlying messages."""
        if len(messages) < self.min_messages_to_summarize:
            if isinstance(cached_summary, dict):
                return SummaryRecord.model_validate(cached_summary)
            return cached_summary

        if not self.should_update_summary(len(messages), cached_summary):
            if isinstance(cached_summary, dict):
                return SummaryRecord.model_validate(cached_summary)
            return cached_summary

        next_version = 1
        if cached_summary:
            prev_v = (
                cached_summary.version
                if isinstance(cached_summary, SummaryRecord)
                else cached_summary.get("version", 0)
            )
            next_version = prev_v + 1

        # Use heuristic/extractive summarization as robust fallback
        try:
            summary = self.extract_structured_summary_heuristics(
                messages=messages,
                conversation_id=conversation_id,
                user_id=user_id,
                version=next_version,
            )
            return summary
        except Exception as exc:
            logger.error("ConversationSummarizer failed for conversation %s: %s", conversation_id, exc)
            if isinstance(cached_summary, dict):
                return SummaryRecord.model_validate(cached_summary)
            return cached_summary

