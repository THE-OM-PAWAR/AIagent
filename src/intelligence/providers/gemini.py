"""Google Gemini API client and conversational session management provider with rate limiting."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
from typing import TYPE_CHECKING, Any
import uuid

from google import genai
from google.genai import types

from src.intelligence.providers.rate_limiter import (
    GeminiAPIError,
    GeminiAuthError,
    GeminiError,
    GeminiInvalidRequestError,
    GeminiRateLimiter,
    GeminiRateLimitError,
    GeminiServerError,
    GeminiTimeoutError,
    classify_gemini_error,
    sanitize_secrets,
)

if TYPE_CHECKING:
    from src.tools.schemas import ToolCall, ToolResult

logger = logging.getLogger(__name__)


class GeminiClient:
    """Manages Google Gemini SDK client and conversational sessions."""

    def __init__(
        self,
        api_key: str,
        model: str,
        system_instruction: str,
        timeout: float = 60.0,
        rate_limiter: GeminiRateLimiter | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._system_instruction = system_instruction
        self._timeout = timeout
        self._rate_limiter = rate_limiter or GeminiRateLimiter(
            rpm_limit=30,
            max_concurrency=3,
            max_retries=3,
            initial_backoff_seconds=1.0,
            max_backoff_seconds=30.0,
        )
        self._client: genai.Client | None = None
        self._sessions: dict[int, Any] = {}

    @property
    def rate_limiter(self) -> GeminiRateLimiter:
        """Return the rate limiter instance."""
        return self._rate_limiter

    def get_telemetry(self) -> dict[str, Any]:
        """Return current rate limiting and request telemetry."""
        return self._rate_limiter.telemetry.get_summary()

    def initialize(self) -> None:
        """Initialize the Google GenAI client instance."""
        logger.info("Initializing Google Gemini client with model: %s", self._model)
        self._client = genai.Client(api_key=self._api_key)
        logger.info("Google Gemini client initialized successfully.")

    @property
    def client(self) -> genai.Client:
        """Return the initialized GenAI client instance.

        Raises:
            RuntimeError: If initialize() has not been called.
        """
        if self._client is None:
            raise RuntimeError("GeminiClient is not initialized. Call initialize() first.")
        return self._client

    @property
    def model(self) -> str:
        """Return the configured model name."""
        return self._model

    def get_or_create_chat(self, user_id: int) -> Any:
        """Get existing conversational chat for a user or create a new one.

        Args:
            user_id: Telegram user ID.

        Returns:
            An active AsyncChat instance for the user.
        """
        if user_id not in self._sessions:
            logger.info("Creating new in-memory Gemini chat session for user: %s", user_id)
            config = types.GenerateContentConfig(
                system_instruction=self._system_instruction,
            )
            chat = self.client.aio.chats.create(model=self._model, config=config)
            self._sessions[user_id] = chat
        return self._sessions[user_id]

    def reset_chat(self, user_id: int) -> bool:
        """Reset the conversational session for a given user.

        Args:
            user_id: Telegram user ID.

        Returns:
            True if an existing session was cleared, False otherwise.
        """
        if user_id in self._sessions:
            logger.info("Resetting Gemini chat session for user: %s", user_id)
            del self._sessions[user_id]
            return True
        return False

    async def generate_response(
        self,
        user_id: int,
        message_text: str,
        history: list[dict[str, Any]] | None = None,
        system_instruction: str | None = None,
        tools: list[Any] | None = None,
        tool_callback: Callable[[list[ToolCall]], Awaitable[list[ToolResult]]] | None = None,
        max_iterations: int = 5,
    ) -> str:
        """Send a user message to Gemini with optional tool calling support.

        Args:
            user_id: Telegram user ID.
            message_text: The user's input text.
            history: Optional list of previous message dictionaries with 'role' and 'content'.
            system_instruction: Optional dynamic system instruction override with RAG context.
            tools: Optional list of google.genai.types.Tool objects.
            tool_callback: Async callback invoked when Gemini requests tool execution.
            max_iterations: Maximum function calling loop iterations.

        Returns:
            Generated response string from Gemini.
        """
        effective_instruction = system_instruction or self._system_instruction

        # Prepare history contents
        history_contents: list[types.Content] = []
        if history is not None:
            valid_history = list(history)
            while valid_history and valid_history[0].get("role") == "model":
                valid_history = valid_history[1:]

            for msg in valid_history:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if content:
                    history_contents.append(
                        types.Content(
                            role=role,
                            parts=[types.Part.from_text(text=content)],
                        )
                    )

        # Build config with tools if provided
        config_kwargs: dict[str, Any] = {
            "system_instruction": effective_instruction,
        }
        if tools:
            config_kwargs["tools"] = tools
            config_kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)

        config = types.GenerateContentConfig(**config_kwargs)

        if history is not None or tools:
            chat = self.client.aio.chats.create(
                model=self._model,
                config=config,
                history=history_contents if history_contents else None,
            )
        else:
            chat = self.get_or_create_chat(user_id)

        async def _call_gemini() -> Any:
            return await asyncio.wait_for(
                chat.send_message(message=message_text),
                timeout=self._timeout,
            )

        response = await self._rate_limiter.execute_with_retry(
            _call_gemini,
            operation_name=f"generate_response (user {user_id})",
        )

        # If tools are enabled, execute the tool calling loop
        if tools and tool_callback:
            from src.tools.schemas import ToolCall, ToolResult
            iteration = 0
            while iteration < max_iterations:
                iteration += 1
                function_calls = getattr(response, "function_calls", None)
                if not function_calls:
                    # Model provided text, finished
                    break

                logger.info(
                    "GeminiClient: user %s triggered %d function call(s) at iteration %d",
                    user_id,
                    len(function_calls),
                    iteration,
                )

                tool_calls: list[ToolCall] = []
                for fc in function_calls:
                    call_id = getattr(fc, "id", None) or f"call_{uuid.uuid4().hex[:8]}"
                    call_name = getattr(fc, "name", "")
                    call_args = getattr(fc, "args", {}) or {}
                    if isinstance(call_args, str):
                        try:
                            call_args = json.loads(call_args)
                        except Exception:
                            call_args = {"raw_input": call_args}
                    tool_calls.append(ToolCall(call_id=call_id, tool_name=call_name, arguments=dict(call_args)))

                # Execute requested tools via callback (executed once)
                tool_results: list[ToolResult] = await tool_callback(tool_calls)

                # Format function responses into Parts
                func_parts: list[types.Part] = []
                for res in tool_results:
                    func_parts.append(
                        types.Part.from_function_response(
                            name=res.tool_name,
                            response=res.to_dict(),
                        )
                    )

                # Send function responses back to Gemini with rate limiting and retry (without repeating tool callback)
                async def _send_tool_feedback() -> Any:
                    return await asyncio.wait_for(
                        chat.send_message(message=func_parts),
                        timeout=self._timeout,
                    )

                response = await self._rate_limiter.execute_with_retry(
                    _send_tool_feedback,
                    operation_name=f"tool_feedback_loop_iter_{iteration} (user {user_id})",
                )

        # Extract text from response
        resp_text = getattr(response, "text", None)
        if not resp_text:
            # Fallback: check candidates
            if response and response.candidates and response.candidates[0].content:
                parts = response.candidates[0].content.parts or []
                text_parts = [p.text for p in parts if getattr(p, "text", None)]
                if text_parts:
                    resp_text = "".join(text_parts)

        if not resp_text or not resp_text.strip():
            logger.warning(
                "Gemini returned empty or blocked response for user %s. Response: %s",
                user_id,
                response,
            )
            raise GeminiAPIError("No response generated by Gemini.")

        return resp_text.strip()


__all__ = [
    "GeminiClient",
    "GeminiError",
    "GeminiTimeoutError",
    "GeminiRateLimitError",
    "GeminiServerError",
    "GeminiAuthError",
    "GeminiInvalidRequestError",
    "GeminiAPIError",
    "GeminiRateLimiter",
    "classify_gemini_error",
    "sanitize_secrets",
]

