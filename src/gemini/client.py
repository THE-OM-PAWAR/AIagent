"""Google Gemini API client and conversational session management."""

import asyncio
import logging
from typing import Any
from google import genai
from google.genai import errors, types

logger = logging.getLogger(__name__)


class GeminiError(Exception):
    """Base exception for Gemini service errors."""
    pass


class GeminiTimeoutError(GeminiError):
    """Raised when request to Gemini times out."""
    pass


class GeminiRateLimitError(GeminiError):
    """Raised when Gemini API rate limit or quota is exceeded."""
    pass


class GeminiAPIError(GeminiError):
    """Raised when Gemini returns an error or empty/filtered response."""
    pass


class GeminiClient:
    """Manages Google Gemini SDK client and conversational sessions."""

    def __init__(
        self,
        api_key: str,
        model: str,
        system_instruction: str,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._system_instruction = system_instruction
        self._timeout = timeout
        self._client: genai.Client | None = None
        self._sessions: dict[int, Any] = {}

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
    ) -> str:
        """Send a user message to Gemini within their conversational context.

        Args:
            user_id: Telegram user ID.
            message_text: The user's input text.
            history: Optional list of previous message dictionaries with 'role' and 'content'.

        Returns:
            Generated response string from Gemini.

        Raises:
            GeminiTimeoutError: If the Gemini request exceeds timeout.
            GeminiRateLimitError: If rate limit or quota is exhausted.
            GeminiAPIError: If the API returns an error or empty content.
            GeminiError: On unexpected exceptions.
        """
        if history is not None:
            # Build conversation history from persisted records
            history_contents: list[types.Content] = []
            valid_history = list(history)

            # Ensure history starts with a user turn if present
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

            config = types.GenerateContentConfig(
                system_instruction=self._system_instruction,
            )
            chat = self.client.aio.chats.create(
                model=self._model,
                config=config,
                history=history_contents if history_contents else None,
            )
        else:
            chat = self.get_or_create_chat(user_id)

        try:
            response = await asyncio.wait_for(
                chat.send_message(message=message_text),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            logger.warning(
                "Gemini request timed out after %.1fs for user: %s",
                self._timeout,
                user_id,
            )
            raise GeminiTimeoutError("Request to Gemini timed out.") from exc
        except errors.APIError as exc:
            code = getattr(exc, "code", None)
            err_str = str(exc).upper()
            if code == 429 or "RESOURCE_EXHAUSTED" in err_str or "QUOTA" in err_str:
                logger.warning("Gemini rate limit exceeded for user %s: %s", user_id, exc)
                raise GeminiRateLimitError("Rate limit exceeded.") from exc
            logger.error("Gemini API error for user %s (code: %s): %s", user_id, code, exc)
            raise GeminiAPIError(f"Gemini API returned error code {code}") from exc
        except Exception as exc:
            logger.error(
                "Unexpected error communicating with Gemini for user %s: %s",
                user_id,
                exc,
                exc_info=True,
            )
            raise GeminiError("Unexpected error communicating with Gemini.") from exc

        if not response or not response.text:
            logger.warning(
                "Gemini returned empty or blocked response for user %s. Response: %s",
                user_id,
                response,
            )
            raise GeminiAPIError("No response generated by Gemini.")

        return response.text
