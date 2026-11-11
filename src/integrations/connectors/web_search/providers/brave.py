"""Brave search provider implementation."""

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


class BraveSearchProvider(BaseSearchProvider):
    """Brave Search API provider."""

    API_URL = "https://api.search.brave.com/res/v1/web/search"

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
        """Execute Brave search query."""
        if not self.api_key:
            raise AuthenticationError("Brave API key is missing.", provider_id="brave")

        clean_query = query.strip()
        if not clean_query:
            return []

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        params: dict[str, Any] = {
            "q": clean_query,
            "count": min(max(1, max_results), 10),
        }

        attempt = 0
        last_exc: Exception | None = None

        while attempt <= self.max_retries:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(self.API_URL, headers=headers, params=params)

                    if resp.status_code == 200:
                        data = resp.json()
                        web_results = data.get("web", {}).get("results", [])
                        items: list[SearchResultItem] = []
                        for r in web_results:
                            title = str(r.get("title", "")).strip() or "Untitled Web Page"
                            url = str(r.get("url", "")).strip()
                            raw_desc = str(r.get("description", ""))
                            clean_snippet = sanitize_web_snippet(raw_desc)
                            items.append(
                                SearchResultItem(
                                    title=title,
                                    url=url,
                                    snippet=clean_snippet,
                                    published_date=r.get("page_age"),
                                    source="brave",
                                )
                            )
                        return items

                    if resp.status_code in (401, 403):
                        raise AuthenticationError("Invalid Brave API key.", provider_id="brave")

                    if resp.status_code == 429:
                        if attempt > self.max_retries:
                            raise RateLimitError("Brave search rate limit exceeded.", provider_id="brave")
                        await asyncio.sleep(1.0 * (2 ** (attempt - 1)))
                        continue

                    if resp.status_code >= 500:
                        if attempt > self.max_retries:
                            raise ProviderUnavailableError(f"Brave server error ({resp.status_code}).", provider_id="brave")
                        await asyncio.sleep(0.5 * attempt)
                        continue

                    raise IntegrationError(f"Brave API error ({resp.status_code}): {resp.text[:200]}", provider_id="brave")

            except (httpx.TimeoutException, httpx.NetworkError) as net_err:
                last_exc = net_err
                if attempt > self.max_retries:
                    raise ProviderUnavailableError(f"Brave connection failed: {net_err}", provider_id="brave") from net_err
                await asyncio.sleep(0.5 * attempt)

        if last_exc:
            raise ProviderUnavailableError(f"Brave request failed: {last_exc}", provider_id="brave") from last_exc
        return []

    async def test_connection(self) -> ConnectionHealth:
        """Verify API key validity against Brave API."""
        if not self.api_key:
            return ConnectionHealth(status="unauthorized", is_healthy=False, error_message="API key is empty.")

        start = time.perf_counter()
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self.API_URL, headers=headers, params={"q": "ping", "count": 1})
                latency = (time.perf_counter() - start) * 1000.0
                if resp.status_code == 200:
                    return ConnectionHealth(status="healthy", is_healthy=True, latency_ms=round(latency, 1))
                if resp.status_code in (401, 403):
                    return ConnectionHealth(status="unauthorized", is_healthy=False, latency_ms=round(latency, 1), error_message="Invalid Brave API key.")
                return ConnectionHealth(status="degraded", is_healthy=False, latency_ms=round(latency, 1), error_message=f"Brave returned status {resp.status_code}.")
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000.0
            return ConnectionHealth(status="unhealthy", is_healthy=False, latency_ms=round(latency, 1), error_message=f"Network error: {exc}")

