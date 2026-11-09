"""Agent execution loop for managing single-turn and multi-turn reasoning steps."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ExecutionLoop:
    """Manages execution cycles between user input, intelligence providers, and tools."""

    def __init__(self, max_iterations: int = 5) -> None:
        self.max_iterations = max_iterations

    async def execute_turn(
        self,
        user_id: int,
        prompt: str,
        intelligence_provider: Any,
        history: list[dict[str, Any]] | None = None,
        tools_registry: Any = None,
    ) -> str:
        """Execute a single interaction turn.

        Args:
            user_id: User identifier.
            prompt: User message prompt.
            intelligence_provider: Model provider client (e.g. GeminiClient).
            history: Optional prepared conversational history.
            tools_registry: Optional tool registry for tool execution.

        Returns:
            Generated response string.
        """
        # Single-step turn delegation to the provider
        return await intelligence_provider.generate_response(
            user_id=user_id,
            message_text=prompt,
            history=history,
        )

