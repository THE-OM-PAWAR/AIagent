"""Tavily search provider implementation with retries, timeouts, and rate limits."""

import asyncio
import logging
import time
from typing import Any

import httpx

from src.integrations.base import ConnectionHealth
from src.integrations.connectors.web_search.providers.base import (
    BaseSearchProvider,
    SearchResultItem,
    sanitize_web_snippet,
)
from src.integrations.errors import (
    AuthenticationError,
    IntegrationError,
    ProviderUnavailableError,
    RateLimitError,
)

logger = logging.getLogger(__name__)


class TavilySearchProvider(BaseSearchProvider):
    """Tavily Search API provider."""

    API_URL = "https://api.tavily.com/search"

    def __init__(
        self,
        api_key: str,
        timeout: float = 15.0,
        max_retries: int = 2,
    ) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.max_retries = max_retries

    async def search(
        self,
        query: str,
        max_results: int = 5,
        topic: str = "general",
        include_domains: list[str] | None = None,
    ) -> list[SearchResultItem]:
        """Execute Tavily search query with bounded retries and sanitized results."""
        if not self.api_key:
            raise AuthenticationError("Tavily API key is missing.", provider_id="tavily")

        clean_query = query.strip()
        if not clean_query:
            return []

        payload: dict[str, Any] = {
            "api_key": self.api_key,
            "query": clean_query,
            "search_depth": "basic",
            "max_results": min(max(1, max_results), 10),
            "include_answer": False,
        }
        if topic in ("news", "finance"):
            payload["topic"] = topic
        if include_domains:
            payload["include_domains"] = include_domains

        attempt = 0
        last_exc: Exception | None = None

        while attempt <= self.max_retries:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(self.API_URL, json=payload)

                    if resp.status_code == 200:
                        data = resp.json()
                        raw_results = data.get("results", [])
                        items: list[SearchResultItem] = []
                        for r in raw_results:
                            title = str(r.get("title", "")).strip() or "Untitled Web Page"
                            url = str(r.get("url", "")).strip()
                            raw_content = str(r.get("content", ""))
                            clean_snippet = sanitize_web_snippet(raw_content)
                            score = float(r.get("score", 0.0) or 0.0)
                            items.append(
                                SearchResultItem(
                                    title=title,
                                    url=url,
                                    snippet=clean_snippet,
                                    score=score,
                                    published_date=r.get("published_date"),
                                    source="tavily",
                                )
                            )
                        return items

                    if resp.status_code in (401, 403):
                        logger.warning("Tavily authentication failed (status: %d)", resp.status_code)
                        raise AuthenticationError("Invalid Tavily API key.", provider_id="tavily")

                    if resp.status_code == 429:
                        logger.warning("Tavily rate limit exceeded on attempt %d", attempt)
                        if attempt > self.max_retries:
                            raise RateLimitError("Tavily search rate limit exceeded.", provider_id="tavily")
                        await asyncio.sleep(1.0 * (2 ** (attempt - 1)))
                        continue

                    if resp.status_code >= 500:
                        logger.warning("Tavily returned server error %d on attempt %d", resp.status_code, attempt)
                        if attempt > self.max_retries:
                            raise ProviderUnavailableError(f"Tavily service error ({resp.status_code}).", provider_id="tavily")
                        await asyncio.sleep(0.5 * attempt)
                        continue

                    # Unhandled client error
                    err_msg = resp.text[:200]
                    raise IntegrationError(f"Tavily API error ({resp.status_code}): {err_msg}", provider_id="tavily")

            except (httpx.TimeoutException, httpx.NetworkError) as net_err:
                last_exc = net_err
                logger.warning("Tavily network/timeout error on attempt %d: %s", attempt, net_err)
                if attempt > self.max_retries:
                    raise ProviderUnavailableError(f"Tavily connection failed: {net_err}", provider_id="tavily") from net_err
                await asyncio.sleep(0.5 * attempt)

        if last_exc:
            raise ProviderUnavailableError(f"Tavily request failed: {last_exc}", provider_id="tavily") from last_exc
        return []

    async def test_connection(self) -> ConnectionHealth:
        """Verify API key validity by querying Tavily API."""
        if not self.api_key:
            return ConnectionHealth(
                status="unauthorized",
                is_healthy=False,
                error_message="API key is empty.",
            )

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.API_URL,
                    json={
                        "api_key": self.api_key,
                        "query": "test ping",
                        "max_results": 1,
                    },
                )
                latency = (time.perf_counter() - start) * 1000.0
                if resp.status_code == 200:
                    return ConnectionHealth(
                        status="healthy",
                        is_healthy=True,
                        latency_ms=round(latency, 1),
                    )
                if resp.status_code in (401, 403):
                    return ConnectionHealth(
                        status="unauthorized",
                        is_healthy=False,
                        latency_ms=round(latency, 1),
                        error_message="Invalid Tavily API key.",
                    )
                if resp.status_code == 429:
                    return ConnectionHealth(
                        status="rate_limited",
                        is_healthy=False,
                        latency_ms=round(latency, 1),
                        error_message="Tavily API rate limit exceeded.",
                    )
                return ConnectionHealth(
                    status="degraded",
                    is_healthy=False,
                    latency_ms=round(latency, 1),
                    error_message=f"Tavily returned unexpected status {resp.status_code}.",
                )
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000.0
            return ConnectionHealth(
                status="unhealthy",
                is_healthy=False,
                latency_ms=round(latency, 1),
                error_message=f"Network error: {exc}",
            )

