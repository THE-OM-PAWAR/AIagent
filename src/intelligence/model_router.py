"""Model routing and provider resolution."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ModelRouter:
    """Routes requests to the designated intelligence model or provider."""

    def __init__(self, default_model: str = "gemini-2.5-flash") -> None:
        self.default_model = default_model
        self._routes: dict[str, Any] = {}

    def resolve_model(self, task_type: str = "general") -> str:
        """Resolve the model name based on task type.

        Args:
            task_type: Type of task (e.g. 'general', 'coding', 'fast').

        Returns:
            The configured model identifier.
        """
        return self._routes.get(task_type, self.default_model)

    def register_route(self, task_type: str, model_name: str) -> None:
        """Register a specific model route for a task type."""
        self._routes[task_type] = model_name

