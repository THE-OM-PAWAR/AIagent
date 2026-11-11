"""Centralized resilient HTTP client with bounded retries, jitter, rate-limit backoff, and idempotency guards."""

import asyncio
from datetime import datetime, timezone
import email.utils
import logging
import random
import re
import time
from typing import Any

import httpx

from src.integrations.errors import (
    AuthenticationError,
    IntegrationError,
    ProviderUnavailableError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

# Sensitive header names to redact from logs
SENSITIVE_HEADERS = {"authorization", "x-subscription-token", "x-api-key", "api-key", "token"}


def mask_headers_for_logging(headers: dict[str, str] | None) -> dict[str, str]:
    """Return a copy of headers with authorization and secret tokens redacted."""
    if not headers:
        return {}
    clean = {}
    for k, v in headers.items():
        if k.lower() in SENSITIVE_HEADERS:
            clean[k] = "[REDACTED]"
        else:
            clean[k] = v
    return clean


def parse_retry_after(header_value: str | None, default_delay: float = 1.0) -> float:
    """Parse HTTP Retry-After header into seconds (supports delta-seconds and HTTP-date)."""
    if not header_value:
        return default_delay

    val = header_value.strip()
    # 1. Delta-seconds
    try:
        return max(0.1, float(val))
    except ValueError:
        pass

    # 2. IMF-fixdate / RFC 2822 date
    try:
        parsed_tuple = email.utils.parsedate_to_datetime(val)
        if parsed_tuple:
            diff = (parsed_tuple - datetime.now(timezone.utc)).total_seconds()
            return max(0.1, diff)
    except Exception:
        pass

    return default_delay


class IntegrationHttpClient:
    """Production-grade asynchronous HTTP client for external service integrations."""

    def __init__(
        self,
        provider_id: str = "generic",
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        write_timeout: float = 30.0,
        max_retries: int = 3,
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 10.0,
        max_retry_after_threshold: float = 60.0,
    ) -> None:
        self.provider_id = provider_id
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self.max_retries = max_retries
        self.base_backoff = base_backoff_seconds
        self.max_backoff = max_backoff_seconds
        self.max_retry_after_threshold = max_retry_after_threshold

        # Observability metrics
        self._total_requests = 0
        self._successful_requests = 0
        self._failed_requests = 0
        self._retries_performed = 0
        self._rate_limit_events = 0
        self._total_latency_seconds = 0.0

    @property
    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.write_timeout,
            pool=10.0,
        )

    def get_telemetry(self) -> dict[str, Any]:
        """Retrieve telemetry metrics for the client."""
        avg_lat = (
            round(self._total_latency_seconds / self._total_requests, 3)
            if self._total_requests > 0
            else 0.0
        )
        return {
            "provider_id": self.provider_id,
            "total_requests": self._total_requests,
            "successful_requests": self._successful_requests,
            "failed_requests": self._failed_requests,
            "retries_performed": self._retries_performed,
            "rate_limit_events": self._rate_limit_events,
            "average_latency_seconds": avg_lat,
        }

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: Any | None = None,
        content: bytes | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        idempotent: bool | None = None,
    ) -> httpx.Response:
        """Execute resilient HTTP request with bounded retries, jitter, and rate-limit backoff."""
        method_upper = method.upper()
        # Default idempotency: GET, HEAD, OPTIONS, PUT, DELETE are idempotent; POST is not
        if idempotent is None:
            idempotent = method_upper in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE")

        req_timeout = httpx.Timeout(timeout) if timeout else self.timeout
        effective_max_retries = max_retries if max_retries is not None else self.max_retries

        attempt = 0
        last_exception: Exception | None = None
        self._total_requests += 1
        t_start = time.perf_counter()

        while attempt <= effective_max_retries:
            attempt += 1
            masked_headers = mask_headers_for_logging(headers)
            try:
                async with httpx.AsyncClient(timeout=req_timeout) as client:
                    if method_upper == "GET":
                        resp = await client.get(url, headers=headers, params=params)
                    elif method_upper == "POST":
                        resp = await client.post(url, headers=headers, json=json, data=data, content=content)
                    elif method_upper == "PATCH":
                        resp = await client.patch(url, headers=headers, json=json, data=data)
                    elif method_upper == "DELETE":
                        resp = await client.delete(url, headers=headers, params=params)
                    else:
                        resp = await client.request(
                            method=method_upper,
                            url=url,
                            headers=headers,
                            params=params,
                            json=json,
                            data=data,
                            content=content,
                        )

                    duration = time.perf_counter() - t_start
                    self._total_latency_seconds += duration

                    # Success (2xx or 3xx)
                    if resp.status_code < 400:
                        self._successful_requests += 1
                        return resp

                    # Authentication errors (401, 403): do NOT retry; fail immediately
                    if resp.status_code in (401, 403):
                        self._failed_requests += 1
                        err_text = resp.text[:250]
                        logger.warning(
                            "IntegrationHttpClient: %s 401/403 auth error at %s: %s",
                            self.provider_id,
                            url,
                            err_text,
                        )
                        raise AuthenticationError(
                            f"{self.provider_id} authentication failed: {err_text}",
                            provider_id=self.provider_id,
                        )

                    # Rate limit exceeded (429)
                    if resp.status_code == 429:
                        self._rate_limit_events += 1
                        raw_retry_after = resp.headers.get("retry-after")
                        delay = parse_retry_after(raw_retry_after, default_delay=self.base_backoff * (2 ** (attempt - 1)))
                        # Add jitter
                        delay = min(delay + random.uniform(0.1, 0.4), self.max_backoff)

                        if delay > self.max_retry_after_threshold or attempt > effective_max_retries:
                            self._failed_requests += 1
                            logger.warning(
                                "IntegrationHttpClient: %s rate limit exceeded. Retry delay %.1fs exceeds threshold.",
                                self.provider_id,
                                delay,
                            )
                            raise RateLimitError(
                                f"{self.provider_id} rate limit exceeded (suggested retry in {delay:.1f}s).",
                                provider_id=self.provider_id,
                                retry_after=delay,
                            )

                        logger.warning(
                            "IntegrationHttpClient: %s rate limit (429) on attempt %d/%d. Backing off for %.2fs...",
                            self.provider_id,
                            attempt,
                            effective_max_retries,
                            delay,
                        )
                        self._retries_performed += 1
                        await asyncio.sleep(delay)
                        continue

                    # Server errors (5xx): retry only if idempotent or GET
                    if resp.status_code >= 500:
                        if not idempotent:
                            self._failed_requests += 1
                            logger.warning(
                                "IntegrationHttpClient: %s server error %d on non-idempotent %s request. Aborting to avoid duplicates.",
                                self.provider_id,
                                resp.status_code,
                                method_upper,
                            )
                            raise ProviderUnavailableError(
                                f"{self.provider_id} server error ({resp.status_code}) on non-idempotent operation.",
                                provider_id=self.provider_id,
                            )

                        if attempt > effective_max_retries:
                            self._failed_requests += 1
                            raise ProviderUnavailableError(
                                f"{self.provider_id} service unavailable ({resp.status_code}) after {effective_max_retries} retries.",
                                provider_id=self.provider_id,
                            )

                        # Exponential backoff with jitter
                        delay = min(self.base_backoff * (2 ** (attempt - 1)) + random.uniform(0.05, 0.25), self.max_backoff)
                        logger.warning(
                            "IntegrationHttpClient: %s 5xx error (%d) on attempt %d/%d. Retrying in %.2fs...",
                            self.provider_id,
                            resp.status_code,
                            attempt,
                            effective_max_retries,
                            delay,
                        )
                        self._retries_performed += 1
                        await asyncio.sleep(delay)
                        continue

                    # Unhandled 4xx client errors (400, 404, 422, etc.)
                    self._failed_requests += 1
                    err_msg = resp.text[:250]
                    raise IntegrationError(
                        f"{self.provider_id} API error ({resp.status_code}): {err_msg}",
                        provider_id=self.provider_id,
                    )

            except (httpx.TimeoutException, httpx.NetworkError) as net_err:
                last_exception = net_err
                if not idempotent:
                    self._failed_requests += 1
                    logger.warning(
                        "IntegrationHttpClient: %s network/timeout error (%s) on non-idempotent %s. Not retrying to prevent duplicate execution.",
                        self.provider_id,
                        net_err,
                        method_upper,
                    )
                    raise ProviderUnavailableError(
                        f"{self.provider_id} request timed out on non-idempotent operation: {net_err}",
                        provider_id=self.provider_id,
                    ) from net_err

                if attempt > effective_max_retries:
                    self._failed_requests += 1
                    raise ProviderUnavailableError(
                        f"{self.provider_id} network connection failed after {effective_max_retries} retries: {net_err}",
                        provider_id=self.provider_id,
                    ) from net_err

                delay = min(self.base_backoff * (2 ** (attempt - 1)) + random.uniform(0.05, 0.25), self.max_backoff)
                logger.warning(
                    "IntegrationHttpClient: %s transient network error on attempt %d/%d (%s). Retrying in %.2fs...",
                    self.provider_id,
                    attempt,
                    effective_max_retries,
                    net_err,
                    delay,
                )
                self._retries_performed += 1
                await asyncio.sleep(delay)

        self._failed_requests += 1
        raise ProviderUnavailableError(
            f"{self.provider_id} request failed after retries: {last_exception}",
            provider_id=self.provider_id,
        ) from last_exception

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)
