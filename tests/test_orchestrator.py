"""Unit tests for the Agent Orchestration Layer and Context Builder."""

import unittest
from unittest.mock import AsyncMock, MagicMock

from src.core.context_builder import ContextBuilder
from src.core.orchestrator import AgentOrchestrator
from src.database import DatabaseClient
from src.intelligence.providers.gemini import (
    GeminiClient,
    GeminiRateLimitError,
    GeminiTimeoutError,
)


class TestContextBuilder(unittest.TestCase):
    """Test message history sanitization and context building."""

    def test_empty_history(self) -> None:
        builder = ContextBuilder(history_limit=10)
        self.assertEqual(builder.build_context([]), [])
        self.assertEqual(builder.build_context(None), [])

    def test_truncation_to_limit(self) -> None:
        builder = ContextBuilder(history_limit=3)
        raw_history = [
            {"role": "user", "content": "1"},
            {"role": "model", "content": "2"},
            {"role": "user", "content": "3"},
            {"role": "model", "content": "4"},
            {"role": "user", "content": "5"},
        ]
        context = builder.build_context(raw_history)
        self.assertEqual(len(context), 3)
        self.assertEqual(context[0], {"role": "user", "content": "3"})
        self.assertEqual(context[1], {"role": "model", "content": "4"})
        self.assertEqual(context[2], {"role": "user", "content": "5"})

    def test_strip_leading_model_turns(self) -> None:
        builder = ContextBuilder(history_limit=10)
        raw_history = [
            {"role": "model", "content": "orphan model message"},
            {"role": "user", "content": "real user prompt"},
            {"role": "assistant", "content": "assistant response"},
        ]
        context = builder.build_context(raw_history)
        self.assertEqual(len(context), 2)
        self.assertEqual(context[0], {"role": "user", "content": "real user prompt"})
        self.assertEqual(context[1], {"role": "model", "content": "assistant response"})


class TestAgentOrchestrator(unittest.IsolatedAsyncioTestCase):
    """Test end-to-end orchestration flow between DB, Context, and Gemini."""

    def setUp(self) -> None:
        self.gemini_mock = AsyncMock(spec=GeminiClient)
        self.db_mock = AsyncMock(spec=DatabaseClient)
        self.orchestrator = AgentOrchestrator(
            gemini_client=self.gemini_mock,
            db_client=self.db_mock,
            context_limit=5,
        )

    async def test_full_message_flow(self) -> None:
        self.db_mock.get_or_create_active_conversation.return_value = {
            "conversation_id": "test_conv_99",
            "user_id": 12345,
        }
        self.db_mock.get_recent_messages.return_value = [
            {"role": "user", "content": "Hi earlier"},
            {"role": "model", "content": "Hello!"},
        ]
        self.gemini_mock.generate_response.return_value = "I am ready to help."

        response = await self.orchestrator.process_message(
            user_id=12345,
            user_text="What is on the agenda?",
        )

        self.assertEqual(response, "I am ready to help.")
        self.db_mock.get_or_create_active_conversation.assert_awaited_once_with(12345)
        self.db_mock.get_recent_messages.assert_awaited_once_with(
            conversation_id="test_conv_99",
            limit=5,
        )
        self.gemini_mock.generate_response.assert_awaited_once_with(
            user_id=12345,
            message_text="What is on the agenda?",
            history=[
                {"role": "user", "content": "Hi earlier"},
                {"role": "model", "content": "Hello!"},
            ],
        )
        self.db_mock.save_turn.assert_awaited_once_with(
            conversation_id="test_conv_99",
            user_id=12345,
            user_message="What is on the agenda?",
            model_response="I am ready to help.",
        )

    async def test_database_history_failure_graceful_fallback(self) -> None:
        self.db_mock.get_or_create_active_conversation.side_effect = Exception("DB connection timeout")
        self.gemini_mock.generate_response.return_value = "Response without history."

        response = await self.orchestrator.process_message(
            user_id=12345,
            user_text="Hello without DB",
        )

        self.assertEqual(response, "Response without history.")
        # Verified fallback called Gemini with empty history
        self.gemini_mock.generate_response.assert_awaited_once_with(
            user_id=12345,
            message_text="Hello without DB",
            history=[],
        )

    async def test_gemini_error_propagation(self) -> None:
        self.db_mock.get_or_create_active_conversation.return_value = {
            "conversation_id": "test_conv_1",
            "user_id": 12345,
        }
        self.db_mock.get_recent_messages.return_value = []
        self.gemini_mock.generate_response.side_effect = GeminiRateLimitError("Rate limit exceeded")

        with self.assertRaises(GeminiRateLimitError):
            await self.orchestrator.process_message(
                user_id=12345,
                user_text="Trigger rate limit",
            )
        self.db_mock.save_turn.assert_not_called()

    async def test_clear_conversation(self) -> None:
        self.db_mock.clear_active_conversation.return_value = True
        self.gemini_mock.reset_chat = MagicMock()

        cleared = await self.orchestrator.clear_conversation(12345)
        self.assertTrue(cleared)
        self.db_mock.clear_active_conversation.assert_awaited_once_with(12345)
        self.gemini_mock.reset_chat.assert_called_once_with(12345)


if __name__ == "__main__":
    unittest.main()

