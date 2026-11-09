"""Context builder for preparing conversation history for LLM providers."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Prepares, sanitizes, and formats conversational context from stored message history."""

    def __init__(self, history_limit: int = 20) -> None:
        self.history_limit = max(1, history_limit)

    def build_context(
        self,
        raw_messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, str]]:
        """Filter, format, and truncate conversation history for Gemini.

        Args:
            raw_messages: Chronological message records retrieved from MongoDB.

        Returns:
            Sanitized list of message dictionaries with 'role' ('user' or 'model') and 'content'.
        """
        if not raw_messages:
            return []

        # Apply history limit window (most recent turns)
        truncated = (
            raw_messages[-self.history_limit :]
            if len(raw_messages) > self.history_limit
            else list(raw_messages)
        )

        formatted_turns: list[dict[str, str]] = []
        for msg in truncated:
            content = str(msg.get("content", "")).strip()
            role = str(msg.get("role", "user")).lower()

            if not content:
                continue

            # Normalize roles to standard 'user' or 'model'
            normalized_role = "model" if role in {"model", "assistant", "bot"} else "user"
            formatted_turns.append({"role": normalized_role, "content": content})

        # Gemini conversational history must start with a user turn
        while formatted_turns and formatted_turns[0]["role"] == "model":
            formatted_turns.pop(0)

        logger.debug(
            "Prepared context with %d turns (from %d raw messages, limit=%d)",
            len(formatted_turns),
            len(raw_messages),
            self.history_limit,
        )
        return formatted_turns

    def prepare_history(
        self,
        raw_messages: list[dict[str, Any]],
        system_instruction: str | None = None,
    ) -> list[dict[str, Any]]:
        """Backward-compatible alias for build_context."""
        return self.build_context(raw_messages)
