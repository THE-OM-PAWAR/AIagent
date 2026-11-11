"""Google Maps Places tool implementations exposing BaseTool interfaces."""

import logging
from typing import Any

from src.integrations.connectors.maps.client import GooglePlacesClient
from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)


async def _resolve_client(
    client: GooglePlacesClient | None,
    user_id: int | None,
    integration_registry: Any | None,
    database_url: str | None,
) -> GooglePlacesClient | None:
    """Resolve active GooglePlacesClient dynamically using user credentials if available."""
    if user_id and integration_registry and database_url:
        try:
            from src.database.repositories.integration_repo import IntegrationRepository
            from src.database.session import get_async_session

            async with get_async_session(database_url) as session:
                repo = IntegrationRepository(session)
                creds = await integration_registry.get_credentials(repo, user_id, "google_maps")
                if creds and creds.get("api_key"):
                    return GooglePlacesClient(api_key=creds["api_key"])
        except Exception as exc:
            logger.debug("Failed resolving user Google Maps credentials: %s", exc)

    return client


class PlacesSearchTool(BaseTool):
    """Search businesses and places via Google Maps Places API."""

    def __init__(
        self,
        client: GooglePlacesClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "places_search_businesses"

    @property
    def description(self) -> str:
        return "Search local businesses, points of interest, and establishments using Google Maps Places API."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (e.g. 'dental clinics in Austin', 'roofing contractors')."},
                "location": {"type": "string", "description": "Optional latitude,longitude string (e.g. '30.2672,-97.7431')."},
                "radius": {"type": "integer", "description": "Search radius in meters (default: 5000, max: 50000).", "default": 5000},
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        location: str | None = None,
        radius: int = 5000,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.api_key:
            return {"error": "Google Maps Places is not connected. Use /connect google_maps in Telegram to configure your API key."}

        places = await active_client.text_search(query=query, location=location, radius=radius)
        return {
            "query": query,
            "results_count": len(places),
            "places": places,
            "attribution": "Powered by Google",
        }


class PlacesGetDetailsTool(BaseTool):
    """Retrieve full place details including phone, address, and website."""

    def __init__(
        self,
        client: GooglePlacesClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "places_get_details"

    @property
    def description(self) -> str:
        return "Retrieve complete business details (address, phone number, website, rating, opening hours) for a Google place_id."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "place_id": {"type": "string", "description": "Unique Google Maps place_id."},
            },
            "required": ["place_id"],
        }

    async def execute(self, place_id: str, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.api_key:
            return {"error": "Google Maps Places is not connected. Use /connect google_maps in Telegram to configure your API key."}

        return await active_client.get_place_details(place_id=place_id)


class PlacesBusinessDiscoveryTool(BaseTool):
    """Discover qualified business leads with verified contact info for client outreach."""

    def __init__(
        self,
        client: GooglePlacesClient | None = None,
        integration_registry: Any | None = None,
        database_url: str | None = None,
    ) -> None:
        self._client = client
        self._integration_registry = integration_registry
        self._database_url = database_url

    @property
    def name(self) -> str:
        return "places_discover_leads"

    @property
    def description(self) -> str:
        return (
            "Client discovery workflow: identify local businesses by industry and city, "
            "extracting phone numbers, websites, addresses, and customer ratings for outreach."
        )

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Industry or niche (e.g. 'accounting firms', 'plumbing contractors', 'gyms')."},
                "location": {"type": "string", "description": "City or geographic area (e.g. 'Austin, TX', 'Miami, FL')."},
                "min_rating": {"type": "number", "description": "Minimum customer rating (e.g. 3.5, 4.0).", "default": 3.5},
                "max_results": {"type": "integer", "description": "Maximum qualified leads to return (default: 5, max: 15).", "default": 5},
            },
            "required": ["category", "location"],
        }

    async def execute(
        self,
        category: str,
        location: str,
        min_rating: float = 3.5,
        max_results: int = 5,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        active_client = await _resolve_client(self._client, user_id, self._integration_registry, self._database_url)
        if not active_client or not active_client.api_key:
            return {"error": "Google Maps Places is not connected. Use /connect google_maps in Telegram to configure your API key."}

        safe_max = min(max(1, max_results), 15)
        leads = await active_client.discover_client_leads(
            category=category,
            location=location,
            min_rating=float(min_rating),
            max_results=safe_max,
        )

        return {
            "category": category,
            "location": location,
            "leads_count": len(leads),
            "leads": leads,
            "attribution": "Powered by Google Places",
        }


def get_maps_tools(
    api_key: str | None = None,
    client: GooglePlacesClient | None = None,
    integration_registry: Any | None = None,
    database_url: str | None = None,
) -> list[BaseTool]:
    """Factory creating all Google Maps Places tools."""
    places_client = client or (GooglePlacesClient(api_key=api_key) if api_key else None)
    return [
        PlacesSearchTool(client=places_client, integration_registry=integration_registry, database_url=database_url),
        PlacesGetDetailsTool(client=places_client, integration_registry=integration_registry, database_url=database_url),
        PlacesBusinessDiscoveryTool(client=places_client, integration_registry=integration_registry, database_url=database_url),
    ]

