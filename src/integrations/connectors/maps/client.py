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
from src.integrations.http_client import IntegrationHttpClient

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
        http_client: IntegrationHttpClient | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.http_client = http_client or IntegrationHttpClient(
            provider_id="google_maps",
            read_timeout=timeout,
        )

    def get_telemetry(self) -> dict[str, Any]:
        """Return production HTTP metrics and latency for Google Maps Places."""
        return self.http_client.get_telemetry()

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
            resp = await self.http_client.get(f"{PLACES_API_BASE}/textsearch/json", params=params, timeout=10.0)
            latency = round((time.perf_counter() - start) * 1000.0, 1)

            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "UNKNOWN")
                if status in ("OK", "ZERO_RESULTS"):
                    return ConnectionHealth(status="healthy", is_healthy=True, latency_ms=latency)
                
                err_msg = data.get("error_message", status)
                # Seamless fallback to Places API (New) if legacy Places API is not activated
                if "legacy" in (err_msg or "").lower() or "legacy" in status.lower():
                    try:
                        new_headers = {
                            "X-Goog-Api-Key": self.api_key,
                            "X-Goog-FieldMask": "places.id,places.displayName",
                            "Content-Type": "application/json",
                        }
                        new_resp = await self.http_client.post(
                            "https://places.googleapis.com/v1/places:searchText",
                            headers=new_headers,
                            json={"textQuery": "restaurant"},
                            timeout=10.0,
                        )
                        if new_resp.status_code == 200:
                            return ConnectionHealth(status="healthy", is_healthy=True, latency_ms=latency)
                    except Exception as fb_err:
                        logger.debug("Places API (New) healthcheck fallback: %s", fb_err)

                if status in ("REQUEST_DENIED", "OVER_QUERY_LIMIT"):
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

        resp = await self.http_client.get(f"{PLACES_API_BASE}/textsearch/json", params=params)
        if resp.status_code != 200:
            raise ProviderUnavailableError(f"Google Places HTTP {resp.status_code}", provider_id="google_maps")

        data = resp.json()
        status = data.get("status", "OK")
        err_msg = data.get("error_message", "")

        # Seamless fallback to Places API (New) if project only enabled Places API (New)
        if status not in ("OK", "ZERO_RESULTS") and "legacy" in (err_msg or "").lower():
            try:
                new_headers = {
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.rating,places.userRatingCount,places.websiteUri,places.nationalPhoneNumber",
                    "Content-Type": "application/json",
                }
                new_resp = await self.http_client.post(
                    "https://places.googleapis.com/v1/places:searchText",
                    headers=new_headers,
                    json={"textQuery": query.strip()},
                )
                if new_resp.status_code == 200:
                    new_data = new_resp.json()
                    new_places = new_data.get("places", [])
                    items = []
                    for np in new_places:
                        items.append({
                            "place_id": np.get("id"),
                            "name": np.get("displayName", {}).get("text", ""),
                            "formatted_address": np.get("formattedAddress"),
                            "rating": np.get("rating"),
                            "user_ratings_total": np.get("userRatingCount"),
                            "website": np.get("websiteUri"),
                            "phone": np.get("nationalPhoneNumber"),
                            "business_status": "OPERATIONAL",
                            "types": [],
                        })
                    return items
            except Exception as fb_err:
                logger.debug("Places API (New) text search fallback: %s", fb_err)

        if status not in ("OK", "ZERO_RESULTS"):
            self._handle_response_error(status, err_msg)

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

        resp = await self.http_client.get(f"{PLACES_API_BASE}/details/json", params=params)
        if resp.status_code != 200:
            raise ProviderUnavailableError(f"Google Places HTTP {resp.status_code}", provider_id="google_maps")

        data = resp.json()
        status = data.get("status", "OK")
        err_msg = data.get("error_message", "")

        # Seamless fallback to Places API (New) if project only enabled Places API (New)
        if status not in ("OK", "ZERO_RESULTS") and "legacy" in (err_msg or "").lower():
            try:
                new_headers = {
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": "id,displayName,formattedAddress,rating,userRatingCount,websiteUri,nationalPhoneNumber,googleMapsUri",
                }
                new_resp = await self.http_client.get(
                    f"https://places.googleapis.com/v1/places/{place_id.strip()}",
                    headers=new_headers,
                )
                if new_resp.status_code == 200:
                    np = new_resp.json()
                    return {
                        "place_id": np.get("id") or place_id,
                        "name": np.get("displayName", {}).get("text", ""),
                        "formatted_address": np.get("formattedAddress"),
                        "phone_number": np.get("nationalPhoneNumber"),
                        "website": np.get("websiteUri"),
                        "rating": np.get("rating"),
                        "user_ratings_total": np.get("userRatingCount"),
                        "business_status": "OPERATIONAL",
                        "google_maps_url": np.get("googleMapsUri"),
                        "attribution": "Powered by Google Places (New)",
                    }
            except Exception as fb_err:
                logger.debug("Places API (New) get details fallback: %s", fb_err)

        if status not in ("OK", "ZERO_RESULTS"):
            self._handle_response_error(status, err_msg)

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

