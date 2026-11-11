"""Web Search connector implementation implementing BaseConnector."""

import logging
from typing import Any

from src.integrations.base import AuthType, BaseConnector, ConnectionHealth, ProviderMetadata
from src.integrations.connectors.web_search.providers.base import BaseSearchProvider
from src.integrations.connectors.web_search.providers.brave import BraveSearchProvider
from src.integrations.connectors.web_search.providers.tavily import TavilySearchProvider
from src.integrations.connectors.web_search.tool import WebSearchTool
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class WebSearchConnector(BaseConnector):
    """Production-grade Web Search connector supporting Tavily and Brave Search."""

    def __init__(
        self,
        default_provider: str = "tavily",
        system_api_key: str | None = None,
    ) -> None:
        self.default_provider = default_provider
        self.system_api_key = system_api_key

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id="web_search",
            name="Web Search",
            description="Real-time internet search for news, companies, jobs, and web research via Tavily or Brave.",
            auth_type=AuthType.API_KEY,
            capabilities=[
                "general_web_search",
                "company_research",
                "job_discovery",
                "article_discovery",
            ],
            setup_guide="Get an API key from https://app.tavily.com (recommended) or https://brave.com/search/api",
            doc_url="https://docs.tavily.com",
        )

    def _resolve_provider(self, credentials: dict[str, Any]) -> BaseSearchProvider:
        """Resolve concrete search provider from credentials."""
        api_key = credentials.get("api_key") or self.system_api_key or ""
        provider_name = credentials.get("provider") or self.default_provider

        # Auto-detect if key indicates Brave or Tavily
        if provider_name == "brave" or api_key.startswith("BSA"):
            return BraveSearchProvider(api_key=api_key)
        return TavilySearchProvider(api_key=api_key)

    async def test_connection(self, credentials: dict[str, Any]) -> ConnectionHealth:
        """Verify API key validity against the search provider."""
        provider = self._resolve_provider(credentials)
        return await provider.test_connection()

    async def get_tools(self, user_id: int, credentials: dict[str, Any]) -> list[BaseTool]:
        """Export configured WebSearchTool for agent use."""
        provider = self._resolve_provider(credentials)
        return [WebSearchTool(provider=provider, name="web_search")]

