"""Central orchestrator coordinating database, intelligence, and execution flow."""

import logging
from typing import Any

from src.core.context_builder import ContextBuilder
from src.core.execution_loop import ExecutionLoop

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """Coordinates context building, intelligence routing, and execution cycles."""

    def __init__(
        self,
        db_client: Any,
        intelligence_client: Any,
        context_limit: int = 20,
    ) -> None:
        self.db = db_client
        self.intelligence = intelligence_client
        self.context_builder = ContextBuilder(history_limit=context_limit)
        self.execution_loop = ExecutionLoop()

    async def handle_user_message(
        self,
        user_id: int,
        message_text: str,
        conversation_id: str | None = None,
    ) -> str:
        """Handle incoming user message through context retrieval, execution, and persistence.

        Args:
            user_id: Unique user identifier.
            message_text: User message input.
            conversation_id: Active conversation identifier.

        Returns:
            Generated assistant response text.
        """
        # 1. Retrieve recent history if database is available
        history: list[dict[str, Any]] = []
        if self.db is not None and conversation_id is not None:
            raw_history = await self.db.get_recent_messages(
                conversation_id=conversation_id,
                limit=self.context_builder.history_limit,
            )
            history = self.context_builder.prepare_history(raw_history)

        # 2. Execute agent turn
        response_text = await self.execution_loop.execute_turn(
            user_id=user_id,
            prompt=message_text,
            intelligence_provider=self.intelligence,
            history=history if history else None,
        )

        # 3. Persist turns if database is available
        if self.db is not None and conversation_id is not None:
            await self.db.save_message(
                conversation_id=conversation_id,
                user_id=user_id,
                role="user",
                content=message_text,
            )
            await self.db.save_message(
                conversation_id=conversation_id,
                user_id=user_id,
                role="model",
                content=response_text,
            )

        return response_text

