"""Centralized asynchronous rate limiter, retry engine, and error classifier for Gemini API."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import random
import re
import time
from typing import Any, TypeVar

from google.genai import errors

logger = logging.getLogger(__name__)

T = TypeVar("T")

# API Key and token masking patterns to prevent logging sensitive secrets
_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z-_]{35}"),
    re.compile(r"AQ\.[0-9A-Za-z-_]{40,}"),
    re.compile(r"x-goog-api-key['\":\s=]+([^\s,'\"]+)", re.IGNORECASE),
    re.compile(r"key=([^\s&]+)", re.IGNORECASE),
    re.compile(r"Bearer\s+([A-Za-z0-9._\-]+)", re.IGNORECASE),
]


def sanitize_secrets(text: Any) -> str:
    """Sanitize API keys and sensitive tokens from log messages and exception strings."""
    s = str(text)
    for pat in _SECRET_PATTERNS:
        s = pat.sub("[REDACTED_SECRET]", s)
    return s


def parse_retry_after(exc: Exception) -> float | None:
    """Extract Retry-After header or duration from exception message/response."""
    # 1. Try response headers
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers:
            retry_val = headers.get("retry-after") or headers.get("Retry-After")
            if retry_val:
                try:
                    return float(retry_val)
                except ValueError:
                    pass

    # 2. Try parsing from error message string (e.g. "Please retry after 15s" or "retry in 8.5s")
    msg = str(exc)
    match = re.search(r"(?:retry\s+(?:in|after)\s+|wait\s+)(\d+(?:\.\d+)?)\s*s?", msg, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except (ValueError, IndexError):
            pass
    return None


class GeminiError(Exception):
    """Base exception for Gemini service errors."""
    pass


class GeminiTimeoutError(GeminiError):
    """Raised when request to Gemini times out."""
    pass


class GeminiRateLimitError(GeminiError):
    """Raised when Gemini API rate limit or quota is exceeded (HTTP 429 / RESOURCE_EXHAUSTED)."""

    def __init__(self, message: str = "Rate limit exceeded.", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GeminiServerError(GeminiError):
    """Raised when Gemini returns a 5xx server-side error (HTTP 500, 502, 503, 504)."""

    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


class GeminiAuthError(GeminiError):
    """Raised when Gemini API authentication or permission fails (HTTP 401, 403)."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


class GeminiInvalidRequestError(GeminiError):
    """Raised when request arguments, structure, or payload are invalid (HTTP 400)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class GeminiAPIError(GeminiError):
    """General error when Gemini returns an error or empty/filtered response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def classify_gemini_error(exc: Exception) -> GeminiError:
    """Classify SDK and network exceptions into structured GeminiError subclasses."""
    if isinstance(exc, GeminiError):
        return exc

    if isinstance(exc, asyncio.TimeoutError):
        return GeminiTimeoutError("Request to Gemini timed out.")

    # Check for Google GenAI API errors
    if isinstance(exc, errors.APIError):
        code = getattr(exc, "code", None)
        status = str(getattr(exc, "status", "") or "").upper()
        raw_msg = getattr(exc, "message", None) or str(exc)
        sanitized_msg = sanitize_secrets(raw_msg)
        retry_after = parse_retry_after(exc)

        # 429 / Rate Limit
        if code == 429 or "RESOURCE_EXHAUSTED" in status or "QUOTA" in sanitized_msg.upper() or "RATE LIMIT" in sanitized_msg.upper():
            return GeminiRateLimitError(
                message=f"Gemini API rate limit or quota exceeded: {sanitized_msg}",
                retry_after=retry_after,
            )

        # 401 / 403 Auth Error
        if code in (401, 403) or "UNAUTHENTICATED" in status or "PERMISSION_DENIED" in status:
            return GeminiAuthError(
                message=f"Gemini API authentication failed ({code}): {sanitized_msg}",
                status_code=code,
            )

        # 400 Invalid Request
        if code == 400 or "INVALID_ARGUMENT" in status:
            return GeminiInvalidRequestError(
                message=f"Gemini API invalid request ({code}): {sanitized_msg}",
                status_code=code,
            )

        # 5xx Server Error
        if (code is not None and 500 <= code < 600) or isinstance(exc, errors.ServerError) or "UNAVAILABLE" in status or "INTERNAL" in status:
            return GeminiServerError(
                message=f"Gemini server error ({code}): {sanitized_msg}",
                status_code=code or 500,
            )

        return GeminiAPIError(
            message=f"Gemini API error ({code}): {sanitized_msg}",
            status_code=code,
        )

    # Network / socket / httpx connection errors
    err_str = sanitize_secrets(str(exc))
    err_type = type(exc).__name__
    if any(k in err_type.lower() or k in err_str.lower() for k in ("connection", "connect", "timeout", "network", "reset")):
        return GeminiServerError(f"Network error connecting to Gemini ({err_type}): {err_str}", status_code=503)

    return GeminiError(f"Unexpected error communicating with Gemini ({err_type}): {err_str}")


@dataclass
class GeminiTelemetry:
    """Telemetry metrics tracking request counts, token usage, latency, and rate-limit events."""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    rate_limit_events: int = 0
    server_error_events: int = 0
    retries_performed: int = 0
    total_backoff_sleep_seconds: float = 0.0
    current_inflight_requests: int = 0
    total_latency_seconds: float = 0.0
    max_latency_seconds: float = 0.0
    recent_latencies: list[float] = field(default_factory=list)

    def record_start(self) -> None:
        self.total_requests += 1
        self.current_inflight_requests += 1

    def record_success(self, duration: float) -> None:
        self.current_inflight_requests = max(0, self.current_inflight_requests - 1)
        self.successful_requests += 1
        self.total_latency_seconds += duration
        self.max_latency_seconds = max(self.max_latency_seconds, duration)
        self.recent_latencies.append(round(duration, 3))
        if len(self.recent_latencies) > 50:
            self.recent_latencies.pop(0)

    def record_failure(self, exc: Exception) -> None:
        self.current_inflight_requests = max(0, self.current_inflight_requests - 1)
        self.failed_requests += 1
        if isinstance(exc, GeminiRateLimitError):
            self.rate_limit_events += 1
        elif isinstance(exc, GeminiServerError):
            self.server_error_events += 1

    def record_retry(self, sleep_time: float, exc: Exception) -> None:
        self.retries_performed += 1
        self.total_backoff_sleep_seconds += sleep_time
        if isinstance(exc, GeminiRateLimitError):
            self.rate_limit_events += 1
        elif isinstance(exc, GeminiServerError):
            self.server_error_events += 1

    def get_summary(self) -> dict[str, Any]:
        avg_latency = (
            round(self.total_latency_seconds / self.successful_requests, 3)
            if self.successful_requests > 0
            else 0.0
        )
        return {
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "inflight_requests": self.current_inflight_requests,
            "rate_limit_events": self.rate_limit_events,
            "server_error_events": self.server_error_events,
            "retries_performed": self.retries_performed,
            "total_backoff_sleep_seconds": round(self.total_backoff_sleep_seconds, 2),
            "average_latency_seconds": avg_latency,
            "max_latency_seconds": round(self.max_latency_seconds, 3),
        }


class GeminiRateLimiter:
    """Centralized asynchronous rate limiter and retry engine for Gemini requests."""

    def __init__(
        self,
        rpm_limit: int = 30,
        max_concurrency: int = 3,
        max_retries: int = 3,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        jitter_factor: float = 0.25,
    ) -> None:
        self.rpm_limit = max(1, rpm_limit)
        self.max_concurrency = max(1, max_concurrency)
        self.max_retries = max(0, max_retries)
        self.initial_backoff = initial_backoff_seconds
        self.max_backoff = max_backoff_seconds
        self.jitter_factor = jitter_factor

        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._rate_lock = asyncio.Lock()
        self._timestamps: list[float] = []
        self.telemetry = GeminiTelemetry()

    async def _acquire_rate_slot(self) -> None:
        """Enforce requests-per-minute (RPM) pacing via sliding window."""
        async with self._rate_lock:
            now = time.monotonic()
            window_start = now - 60.0
            # Evict timestamps older than 60 seconds
            self._timestamps = [t for t in self._timestamps if t > window_start]

            if len(self._timestamps) >= self.rpm_limit:
                # Wait until the oldest timestamp slides out of the 60s window
                oldest = self._timestamps[0]
                sleep_needed = max(0.05, 60.0 - (now - oldest) + 0.1)
                logger.info("GeminiRateLimiter: pacing requests (RPM limit %d reached). Sleeping %.2fs", self.rpm_limit, sleep_needed)
                await asyncio.sleep(sleep_needed)
                self._timestamps = [t for t in self._timestamps if t > (time.monotonic() - 60.0)]

            self._timestamps.append(time.monotonic())

    def _is_retryable(self, exc: Exception) -> bool:
        """Determine if an error is transient and safe to retry."""
        if isinstance(exc, (GeminiRateLimitError, GeminiServerError, GeminiTimeoutError)):
            return True
        return False

    def _calculate_backoff(self, attempt: int, exc: Exception) -> float:
        """Calculate exponential backoff with jitter and respect Retry-After if present."""
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None and retry_after > 0:
            base_delay = min(retry_after, self.max_backoff)
        else:
            # Exponential backoff: initial * 2^(attempt - 1)
            base_delay = min(self.initial_backoff * (2.0 ** (attempt - 1)), self.max_backoff)

        # Full jitter: random delay between 0.8 * base_delay and 1.2 * base_delay + uniform random
        jitter_range = base_delay * self.jitter_factor
        jittered = base_delay + random.uniform(-jitter_range, jitter_range) + random.uniform(0.1, 0.5)
        return max(0.2, min(jittered, self.max_backoff))

    async def execute_with_retry(
        self,
        func: Callable[[], Awaitable[T]],
        operation_name: str = "Gemini API request",
    ) -> T:
        """Execute async Gemini operation within concurrency limits, rate slot, and retry loop."""
        attempt = 0
        last_error: Exception | None = None

        while attempt <= self.max_retries:
            attempt += 1

            # 1. Acquire RPM slot
            await self._acquire_rate_slot()

            # 2. Acquire concurrency semaphore
            async with self._semaphore:
                self.telemetry.record_start()
                start_time = time.perf_counter()
                try:
                    result = await func()
                    duration = time.perf_counter() - start_time
                    self.telemetry.record_success(duration)
                    if attempt > 1:
                        logger.info("GeminiRateLimiter: %s succeeded on attempt %d (duration=%.2fs)", operation_name, attempt, duration)
                    return result
                except Exception as raw_exc:
                    duration = time.perf_counter() - start_time
                    classified = classify_gemini_error(raw_exc)
                    last_error = classified

                    if not self._is_retryable(classified) or attempt > self.max_retries:
                        self.telemetry.record_failure(classified)
                        logger.error(
                            "GeminiRateLimiter: %s failed on attempt %d/%d (%s): %s",
                            operation_name,
                            attempt,
                            self.max_retries + 1,
                            type(classified).__name__,
                            sanitize_secrets(classified),
                        )
                        raise classified from raw_exc

                    backoff_delay = self._calculate_backoff(attempt, classified)
                    self.telemetry.record_retry(backoff_delay, classified)
                    logger.warning(
                        "GeminiRateLimiter: %s encountered %s on attempt %d/%d. Retrying in %.2fs. Error: %s",
                        operation_name,
                        type(classified).__name__,
                        attempt,
                        self.max_retries + 1,
                        backoff_delay,
                        sanitize_secrets(classified),
                    )

            # Sleep outside of concurrency semaphore to free up slot for others while backing off
            await asyncio.sleep(backoff_delay)

        assert last_error is not None
        self.telemetry.record_failure(last_error)
        raise last_error

