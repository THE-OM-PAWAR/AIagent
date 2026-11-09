"""Context builder for constructing prompt and history context."""

from typing import Any


class ContextBuilder:
    """Constructs prompt context and manages message history windows."""

    def __init__(self, history_limit: int = 20) -> None:
        self.history_limit = history_limit

    def prepare_history(
        self,
        raw_messages: list[dict[str, Any]],
        system_instruction: str | None = None,
    ) -> list[dict[str, Any]]:
        """Filter, format, and truncate conversation history.

        Args:
            raw_messages: Raw message records from database.
            system_instruction: Optional system instruction override.

        Returns:
            Truncated chronological list of message turns.
        """
        # Truncate to history limit
        truncated = raw_messages[-self.history_limit :] if len(raw_messages) > self.history_limit else list(raw_messages)

        # Ensure valid conversational alternating sequence starting with user
        valid_turns: list[dict[str, Any]] = []
        for msg in truncated:
            content = msg.get("content", "").strip()
            role = msg.get("role", "user")
            if content:
                valid_turns.append({"role": role, "content": content})

        while valid_turns and valid_turns[0].get("role") == "model":
            valid_turns = valid_turns[1:]

        return valid_turns

