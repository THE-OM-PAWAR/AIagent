"""Google Maps Places connector implementing BaseConnector."""

import logging
from typing import Any

from src.integrations.base import AuthType, BaseConnector, ConnectionHealth, ProviderMetadata
from src.integrations.connectors.maps.client import GooglePlacesClient
from src.integrations.connectors.maps.tools import get_maps_tools
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class GooglePlacesConnector(BaseConnector):
    """Connector for Google Maps Places Web Services API."""

    def __init__(
        self,
        system_api_key: str | None = None,
        database_url: str | None = None,
    ) -> None:
        self.system_api_key = system_api_key
        self.database_url = database_url

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id="google_maps",
            name="Google Maps Places",
            description="Find local businesses, retrieve verified phone numbers/websites, and perform lead discovery.",
            auth_type=AuthType.API_KEY,
            capabilities=[
                "search_businesses",
                "get_place_details",
                "extract_contact_info",
                "discover_leads",
            ],
            setup_guide="Obtain a Google Maps Places API key from Google Cloud Console.",
            doc_url="https://developers.google.com/maps/documentation/places/web-service",
        )

    def _resolve_client(self, credentials: dict[str, Any]) -> GooglePlacesClient:
        key = credentials.get("api_key") or self.system_api_key or ""
        return GooglePlacesClient(api_key=key)

    async def test_connection(self, credentials: dict[str, Any]) -> ConnectionHealth:
        client = self._resolve_client(credentials)
        return await client.test_connection()

    async def get_tools(self, user_id: int, credentials: dict[str, Any]) -> list[BaseTool]:
        client = self._resolve_client(credentials)
        return get_maps_tools(client=client, database_url=self.database_url)

