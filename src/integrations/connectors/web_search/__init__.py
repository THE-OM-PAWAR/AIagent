"""Web search connector package."""

from src.integrations.connectors.web_search.connector import WebSearchConnector
from src.integrations.connectors.web_search.providers import (
    BaseSearchProvider,
    BraveSearchProvider,
    SearchResultItem,
    TavilySearchProvider,
    sanitize_web_snippet,
)
from src.integrations.connectors.web_search.tool import WebSearchTool

__all__ = [
    "BaseSearchProvider",
    "BraveSearchProvider",
    "SearchResultItem",
    "TavilySearchProvider",
    "WebSearchConnector",
    "WebSearchTool",
    "sanitize_web_snippet",
]
