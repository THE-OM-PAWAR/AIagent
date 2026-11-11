"""Asynchronous client for Google Maps Places API."""

import logging
import time
from typing import Any

import httpx

from src.integrations.base import ConnectionHealth
from src.integrations.errors import (
    AuthenticationError,
    IntegrationError,
    ProviderUnavailableError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

PLACES_API_BASE = "https://maps.googleapis.com/maps/api/place"

# Optimized field masks for Place Details to control billing costs and respect API guidelines
DEFAULT_PLACE_FIELDS = [
    "place_id",
    "name",
    "formatted_address",
    "formatted_phone_number",
    "international_phone_number",
    "website",
    "rating",
    "user_ratings_total",
    "business_status",
    "opening_hours",
    "geometry",
    "url",
]


class GooglePlacesClient:
    """Asynchronous client interacting with Google Maps Places Web Service API."""

    def __init__(
        self,
        api_key: str,
        timeout: float = 15.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout

    def _handle_response_error(self, status: str, error_message: str | None = None) -> None:
        """Handle Places API specific status codes."""
        msg = error_message or status
        if status in ("REQUEST_DENIED", "OVER_QUERY_LIMIT"):
            if "key" in (error_message or "").lower():
                raise AuthenticationError(f"Google Places API key invalid: {msg}", provider_id="google_maps")
            raise RateLimitError(f"Google Places quota or query limit exceeded: {msg}", provider_id="google_maps")
        if status == "INVALID_REQUEST":
            raise IntegrationError(f"Google Places invalid request parameters: {msg}", provider_id="google_maps")
        raise IntegrationError(f"Google Places API error ({status}): {msg}", provider_id="google_maps")

    async def test_connection(self) -> ConnectionHealth:
        """Verify API key validity by querying Places API."""
        if not self.api_key:
            return ConnectionHealth(status="unauthorized", is_healthy=False, error_message="API key is empty.")

        start = time.perf_counter()
        params = {"query": "restaurant", "key": self.api_key}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{PLACES_API_BASE}/textsearch/json", params=params)
                latency = round((time.perf_counter() - start) * 1000.0, 1)

                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status", "UNKNOWN")
                    if status in ("OK", "ZERO_RESULTS"):
                        return ConnectionHealth(status="healthy", is_healthy=True, latency_ms=latency)
                    if status in ("REQUEST_DENIED", "OVER_QUERY_LIMIT"):
                        err_msg = data.get("error_message", status)
                        return ConnectionHealth(
                            status="unauthorized",
                            is_healthy=False,
                            latency_ms=latency,
                            error_message=err_msg,
                        )

                return ConnectionHealth(
                    status="degraded",
                    is_healthy=False,
                    latency_ms=latency,
                    error_message=f"Places returned HTTP {resp.status_code}",
                )
        except Exception as exc:
            latency = round((time.perf_counter() - start) * 1000.0, 1)
            return ConnectionHealth(
                status="unhealthy",
                is_healthy=False,
                latency_ms=latency,
                error_message=f"Network error verifying Places: {exc}",
            )

    async def text_search(
        self,
        query: str,
        location: str | None = None,
        radius: int = 5000,
        type_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search places by text query and optional geographic context."""
        params: dict[str, Any] = {
            "query": query.strip(),
            "key": self.api_key,
        }
        if location:
            params["location"] = location.strip()
            params["radius"] = min(max(500, radius), 50000)
        if type_filter:
            params["type"] = type_filter.strip()

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{PLACES_API_BASE}/textsearch/json", params=params)
            if resp.status_code != 200:
                raise ProviderUnavailableError(f"Google Places HTTP {resp.status_code}", provider_id="google_maps")

            data = resp.json()
            status = data.get("status", "OK")
            if status not in ("OK", "ZERO_RESULTS"):
                self._handle_response_error(status, data.get("error_message"))

            results = data.get("results", [])
            items = []
            for r in results:
                items.append({
                    "place_id": r.get("place_id"),
                    "name": r.get("name"),
                    "formatted_address": r.get("formatted_address"),
                    "rating": r.get("rating"),
                    "user_ratings_total": r.get("user_ratings_total"),
                    "business_status": r.get("business_status"),
                    "types": r.get("types", []),
                    "lat": r.get("geometry", {}).get("location", {}).get("lat"),
                    "lng": r.get("geometry", {}).get("location", {}).get("lng"),
                })
            return items

    async def get_place_details(
        self,
        place_id: str,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Retrieve place details applying field masks to control billing and bandwidth."""
        mask_fields = fields or DEFAULT_PLACE_FIELDS
        params = {
            "place_id": place_id.strip(),
            "fields": ",".join(mask_fields),
            "key": self.api_key,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{PLACES_API_BASE}/details/json", params=params)
            if resp.status_code != 200:
                raise ProviderUnavailableError(f"Google Places HTTP {resp.status_code}", provider_id="google_maps")

            data = resp.json()
            status = data.get("status", "OK")
            if status not in ("OK", "ZERO_RESULTS"):
                self._handle_response_error(status, data.get("error_message"))

            result = data.get("result", {})
            opening = result.get("opening_hours", {})
            return {
                "place_id": result.get("place_id") or place_id,
                "name": result.get("name"),
                "formatted_address": result.get("formatted_address"),
                "phone_number": result.get("formatted_phone_number") or result.get("international_phone_number"),
                "website": result.get("website"),
                "rating": result.get("rating"),
                "user_ratings_total": result.get("user_ratings_total"),
                "business_status": result.get("business_status"),
                "is_open_now": opening.get("open_now"),
                "weekday_hours": opening.get("weekday_text", []),
                "google_maps_url": result.get("url"),
                "attribution": "Powered by Google Places",
            }

    async def discover_client_leads(
        self,
        category: str,
        location: str,
        min_rating: float = 3.5,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """Client discovery workflow: find qualified businesses with direct contact info."""
        search_query = f"{category} in {location}"
        candidates = await self.text_search(query=search_query)

        leads = []
        for cand in candidates[:max_results]:
            pid = cand.get("place_id")
            if not pid:
                continue

            try:
                details = await self.get_place_details(pid)
                rating = details.get("rating") or 0.0
                if rating >= min_rating:
                    leads.append(details)
            except Exception as exc:
                logger.debug("Failed fetching details for place %s: %s", pid, exc)

        return leads

