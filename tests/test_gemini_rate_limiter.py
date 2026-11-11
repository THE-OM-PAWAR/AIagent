"""Unit tests for Gemini rate limiter, error classifier, retry engine, and error handling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import unittest

from google.genai import errors

from src.intelligence.providers.gemini import (
    GeminiAPIError,
    GeminiAuthError,
    GeminiClient,
    GeminiError,
    GeminiInvalidRequestError,
    GeminiRateLimiter,
    GeminiRateLimitError,
    GeminiServerError,
    GeminiTimeoutError,
    classify_gemini_error,
    sanitize_secrets,
)
from src.intelligence.providers.rate_limiter import parse_retry_after
from src.tools.executor import ToolExecutor
from src.tools.registry import ToolRegistry
from src.tools.schemas import ToolCall, ToolDefinition, ToolResult


def _make_api_error(code: int, message: str, status: str = "") -> errors.APIError:
    return errors.APIError(code, {"error": {"message": message, "status": status}})


class TestGeminiErrorClassifierAndSanitizer(unittest.TestCase):
    """Test classification of errors and sensitive credential masking."""

    def test_sanitize_secrets(self) -> None:
        raw_msg = (
            "Failed with key AIzaSyD1234567890abcdef1234567890abcdef and "
            "Bearer ya29.a0AfH6SMBxyz_12345 and x-goog-api-key: secretkey12345"
        )
        sanitized = sanitize_secrets(raw_msg)
        self.assertNotIn("AIzaSyD1234567890abcdef1234567890abcdef", sanitized)
        self.assertNotIn("secretkey12345", sanitized)
        self.assertIn("[REDACTED_SECRET]", sanitized)

    def test_classify_429_rate_limit(self) -> None:
        exc = _make_api_error(429, "Resource has been exhausted (quota).", "RESOURCE_EXHAUSTED")
        classified = classify_gemini_error(exc)
        self.assertIsInstance(classified, GeminiRateLimitError)
        self.assertIn("rate limit or quota exceeded", str(classified).lower())

    def test_classify_401_403_auth(self) -> None:
        exc_401 = _make_api_error(401, "API key not valid", "UNAUTHENTICATED")
        classified_401 = classify_gemini_error(exc_401)
        self.assertIsInstance(classified_401, GeminiAuthError)
        self.assertEqual(classified_401.status_code, 401)

        exc_403 = _make_api_error(403, "Permission denied", "PERMISSION_DENIED")
        classified_403 = classify_gemini_error(exc_403)
        self.assertIsInstance(classified_403, GeminiAuthError)
        self.assertEqual(classified_403.status_code, 403)

    def test_classify_400_invalid_request(self) -> None:
        exc = _make_api_error(400, "Invalid argument provided", "INVALID_ARGUMENT")
        classified = classify_gemini_error(exc)
        self.assertIsInstance(classified, GeminiInvalidRequestError)
        self.assertEqual(classified.status_code, 400)

    def test_classify_5xx_server_error(self) -> None:
        exc_503 = _make_api_error(503, "Service Unavailable", "UNAVAILABLE")
        classified_503 = classify_gemini_error(exc_503)
        self.assertIsInstance(classified_503, GeminiServerError)
        self.assertEqual(classified_503.status_code, 503)

        exc_500 = _make_api_error(500, "Internal Server Error", "INTERNAL")
        classified_500 = classify_gemini_error(exc_500)
        self.assertIsInstance(classified_500, GeminiServerError)
        self.assertEqual(classified_500.status_code, 500)

    def test_classify_timeout(self) -> None:
        classified = classify_gemini_error(asyncio.TimeoutError())
        self.assertIsInstance(classified, GeminiTimeoutError)

    def test_parse_retry_after(self) -> None:
        # From message string
        exc = Exception("Rate limit exceeded. Please retry after 14.5s")
        self.assertEqual(parse_retry_after(exc), 14.5)

        # From compound string (Google AI Studio actual error format)
        exc_google = Exception(
            "Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, "
            "description: Generate content requests per day limit exceeded on Free Tier. Please retry in 3h48m40.2s."
        )
        self.assertAlmostEqual(parse_retry_after(exc_google), 13720.2, places=1)

        # From headers
        mock_resp = MagicMock()
        mock_resp.headers = {"Retry-After": "25"}
        exc_with_resp = Exception("Error")
        exc_with_resp.response = mock_resp
        self.assertEqual(parse_retry_after(exc_with_resp), 25.0)

    def test_classify_daily_quota_exhaustion(self) -> None:
        msg = (
            "Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
            "limit: 20, model: gemini-3.6-flash, description: Generate content requests per day limit exceeded on Free Tier.\n"
            "Please retry in 3h48m40s."
        )
        exc = _make_api_error(429, msg, "RESOURCE_EXHAUSTED")
        classified = classify_gemini_error(exc)
        self.assertIsInstance(classified, GeminiRateLimitError)
        self.assertTrue(classified.is_daily_quota)
        self.assertAlmostEqual(classified.retry_after, 13720.0, places=0)


class TestGeminiRateLimiter(unittest.IsolatedAsyncioTestCase):
    """Test rate limiter pacing, concurrency semaphore, exponential backoff, and retries."""

    async def test_successful_request_telemetry(self) -> None:
        limiter = GeminiRateLimiter(rpm_limit=60, max_concurrency=2, max_retries=2)
        mock_op = AsyncMock(return_value="model_output")

        result = await limiter.execute_with_retry(mock_op, "test_op")
        self.assertEqual(result, "model_output")
        self.assertEqual(mock_op.call_count, 1)

        summary = limiter.telemetry.get_summary()
        self.assertEqual(summary["total_requests"], 1)
        self.assertEqual(summary["successful_requests"], 1)
        self.assertEqual(summary["failed_requests"], 0)
        self.assertEqual(summary["retries_performed"], 0)

    async def test_non_retryable_error_fails_immediately(self) -> None:
        limiter = GeminiRateLimiter(rpm_limit=60, max_concurrency=2, max_retries=3)
        auth_exc = _make_api_error(401, "Unauthorized API key", "UNAUTHENTICATED")
        mock_op = AsyncMock(side_effect=auth_exc)

        with self.assertRaises(GeminiAuthError):
            await limiter.execute_with_retry(mock_op, "test_auth")

        # Non-retryable error must not retry
        self.assertEqual(mock_op.call_count, 1)
        summary = limiter.telemetry.get_summary()
        self.assertEqual(summary["failed_requests"], 1)
        self.assertEqual(summary["retries_performed"], 0)

    async def test_daily_quota_exhaustion_does_not_retry(self) -> None:
        limiter = GeminiRateLimiter(rpm_limit=60, max_concurrency=2, max_retries=3)
        msg = (
            "Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
            "limit: 20, model: gemini-3.6-flash, description: Generate content requests per day limit exceeded on Free Tier.\n"
            "Please retry in 3h48m40s."
        )
        exc = _make_api_error(429, msg, "RESOURCE_EXHAUSTED")
        mock_op = AsyncMock(side_effect=exc)

        with self.assertRaises(GeminiRateLimitError):
            await limiter.execute_with_retry(mock_op, "test_daily")

        # Must fail on attempt 1 immediately without wasting time retrying
        self.assertEqual(mock_op.call_count, 1)

    async def test_retryable_429_recovers_after_retry(self) -> None:
        limiter = GeminiRateLimiter(
            rpm_limit=60,
            max_concurrency=2,
            max_retries=3,
            initial_backoff_seconds=0.01,
            max_backoff_seconds=0.05,
        )
        rate_exc = _make_api_error(429, "Quota exceeded. retry in 0.01s", "RESOURCE_EXHAUSTED")
        mock_op = AsyncMock(side_effect=[rate_exc, rate_exc, "recovered_success"])

        result = await limiter.execute_with_retry(mock_op, "test_retry")
        self.assertEqual(result, "recovered_success")
        self.assertEqual(mock_op.call_count, 3)

        summary = limiter.telemetry.get_summary()
        self.assertEqual(summary["successful_requests"], 1)
        self.assertEqual(summary["retries_performed"], 2)
        self.assertEqual(summary["rate_limit_events"], 2)

    async def test_exhausted_retries_raises_classified_error(self) -> None:
        limiter = GeminiRateLimiter(
            rpm_limit=60,
            max_concurrency=2,
            max_retries=2,
            initial_backoff_seconds=0.01,
            max_backoff_seconds=0.05,
        )
        server_exc = _make_api_error(503, "Service Unavailable", "UNAVAILABLE")
        mock_op = AsyncMock(side_effect=server_exc)

        with self.assertRaises(GeminiServerError):
            await limiter.execute_with_retry(mock_op, "test_503")

        # 1 initial attempt + 2 retries = 3 attempts total
        self.assertEqual(mock_op.call_count, 3)
        summary = limiter.telemetry.get_summary()
        self.assertEqual(summary["failed_requests"], 1)
        self.assertEqual(summary["retries_performed"], 2)
        self.assertEqual(summary["server_error_events"], 3)

    async def test_concurrency_limit_enforced(self) -> None:
        limiter = GeminiRateLimiter(rpm_limit=100, max_concurrency=2, max_retries=0)
        active_concurrent = 0
        max_seen_concurrent = 0
        lock = asyncio.Lock()

        async def worker() -> str:
            nonlocal active_concurrent, max_seen_concurrent
            async with lock:
                active_concurrent += 1
                if active_concurrent > max_seen_concurrent:
                    max_seen_concurrent = active_concurrent
            await asyncio.sleep(0.05)
            async with lock:
                active_concurrent -= 1
            return "done"

        tasks = [limiter.execute_with_retry(worker) for _ in range(5)]
        results = await asyncio.gather(*tasks)
        self.assertEqual(results, ["done"] * 5)
        self.assertLessEqual(max_seen_concurrent, 2)


class TestToolReExecutionPrevention(unittest.IsolatedAsyncioTestCase):
    """Test requirement 5: tool execution is not repeated when LLM response generation retries."""

    async def test_tool_executor_caches_call_id(self) -> None:
        registry = ToolRegistry()
        call_counter = 0

        def sample_tool(val: int) -> int:
            nonlocal call_counter
            call_counter += 1
            return val * 2

        registry.register(
            name="sample_tool",
            func=sample_tool,
            description="Sample calculation tool",
            parameters_schema={"type": "object", "properties": {"val": {"type": "integer"}}},
        )

        executor = ToolExecutor(registry=registry)

        # First execution
        res1 = await executor.execute_tool(
            tool_name="sample_tool",
            arguments={"val": 5},
            call_id="call_unique_123",
        )
        self.assertTrue(res1.success)
        self.assertEqual(res1.output, 10)
        self.assertEqual(call_counter, 1)

        # Repeat execution with same call_id (simulating a turn retry)
        res2 = await executor.execute_tool(
            tool_name="sample_tool",
            arguments={"val": 5},
            call_id="call_unique_123",
        )
        self.assertTrue(res2.success)
        self.assertEqual(res2.output, 10)
        # Function underlying implementation should NOT be called again
        self.assertEqual(call_counter, 1)

    async def test_gemini_client_tool_feedback_retry_does_not_repeat_tool_callback(self) -> None:
        """Verify that when tool feedback send_message fails with 429 and retries, tool_callback is called only once."""
        fast_limiter = GeminiRateLimiter(
            rpm_limit=60,
            max_concurrency=2,
            max_retries=2,
            initial_backoff_seconds=0.01,
            max_backoff_seconds=0.02,
        )
        client = GeminiClient(
            api_key="test_key",
            model="gemini-2.5-flash",
            system_instruction="sys",
            rate_limiter=fast_limiter,
        )
        client._client = MagicMock()

        # Step 1: Model asks for tool call
        fc_part = MagicMock()
        fc_part.id = "call_abc_456"
        fc_part.name = "sample_tool"
        fc_part.args = {"val": 10}

        resp_with_tools = MagicMock()
        resp_with_tools.function_calls = [fc_part]
        resp_with_tools.text = None

        # Step 2: Tool feedback initially fails with 429, then succeeds with text
        resp_final = MagicMock()
        resp_final.function_calls = []
        resp_final.text = "Here is the final answer: 20"

        rate_limit_err = _make_api_error(429, "Quota exceeded", "RESOURCE_EXHAUSTED")

        mock_chat = AsyncMock()
        # send_message:
        # call 1 (initial message): returns resp_with_tools
        # call 2 (tool feedback attempt 1): raises rate_limit_err
        # call 3 (tool feedback attempt 2 retry): returns resp_final
        mock_chat.send_message = AsyncMock(side_effect=[
            resp_with_tools,
            rate_limit_err,
            resp_final,
        ])
        client.client.aio.chats.create = MagicMock(return_value=mock_chat)

        tool_callback_count = 0

        async def mock_callback(calls: list[ToolCall]) -> list[ToolResult]:
            nonlocal tool_callback_count
            tool_callback_count += 1
            return [ToolResult(call_id=calls[0].call_id, tool_name="sample_tool", success=True, output=20)]

        final_text = await client.generate_response(
            user_id=101,
            message_text="Calculate double of 10",
            tools=[MagicMock()],
            tool_callback=mock_callback,
        )

        self.assertEqual(final_text, "Here is the final answer: 20")
        # Critical verification: tool callback was executed exactly ONCE
        self.assertEqual(tool_callback_count, 1)
        # Send message called 3 times (1 initial + 2 tool feedback attempts)
        self.assertEqual(mock_chat.send_message.call_count, 3)


class TestTelegramHandlerErrorUnwrapping(unittest.IsolatedAsyncioTestCase):
    """Test that Telegram message handler correctly unwraps cause and replies with informative text."""

    async def test_wrapped_rate_limit_unwrapped(self) -> None:
        from src.core.orchestrator import OrchestratorError
        from src.interfaces.telegram.handlers import handle_message

        # Create mock update and context
        mock_update = MagicMock()
        mock_update.effective_user.id = 101
        mock_update.message.message_id = 999
        mock_update.message.chat.id = 12345
        mock_update.message.text = "Hello AI"
        mock_update.message.reply_text = AsyncMock()

        # Mock orchestrator raising OrchestratorError wrapping GeminiRateLimitError
        rate_err = GeminiRateLimitError("Rate limit exceeded.", retry_after=12.0)
        wrapped_err = OrchestratorError("Generation failed")
        wrapped_err.__cause__ = rate_err

        mock_orch = AsyncMock()
        mock_orch.process_message = AsyncMock(side_effect=wrapped_err)

        mock_context = MagicMock()
        mock_context.bot_data = {
            "config": MagicMock(allowed_telegram_user_ids=[101]),
            "orchestrator": mock_orch,
        }

        with patch("src.interfaces.telegram.handlers.is_authorized", return_value=True):
            await handle_message(mock_update, mock_context)

        mock_update.message.reply_text.assert_called_once()
        reply_call_arg = mock_update.message.reply_text.call_args[0][0]
        self.assertIn("rate limit has been reached", reply_call_arg)
        self.assertIn("12s", reply_call_arg)

    async def test_wrapped_daily_quota_shows_hours_and_minutes(self) -> None:
        from src.core.orchestrator import OrchestratorError
        from src.interfaces.telegram.handlers import handle_message

        mock_update = MagicMock()
        mock_update.effective_user.id = 101
        mock_update.message.message_id = 999
        mock_update.message.chat.id = 12345
        mock_update.message.text = "Hello AI"
        mock_update.message.reply_text = AsyncMock()

        rate_err = GeminiRateLimitError("Daily quota exceeded", retry_after=13720.0, is_daily_quota=True)
        wrapped_err = OrchestratorError("Generation failed")
        wrapped_err.__cause__ = rate_err

        mock_orch = AsyncMock()
        mock_orch.process_message = AsyncMock(side_effect=wrapped_err)

        mock_context = MagicMock()
        mock_context.bot_data = {
            "config": MagicMock(allowed_telegram_user_ids=[101]),
            "orchestrator": mock_orch,
        }

        with patch("src.interfaces.telegram.handlers.is_authorized", return_value=True):
            await handle_message(mock_update, mock_context)

        mock_update.message.reply_text.assert_called_once()
        reply_call_arg = mock_update.message.reply_text.call_args[0][0]
        self.assertIn("Daily Quota Exceeded", reply_call_arg)
        self.assertIn("3h 48m", reply_call_arg)


if __name__ == "__main__":
    unittest.main()
