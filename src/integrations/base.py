"""Base interfaces and data structures for external integrations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from src.tools.base import BaseTool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AuthType(str, Enum):
    """Authentication mechanism supported by provider."""
    API_KEY = "api_key"
    OAUTH2 = "oauth2"


@dataclass
class ConnectionHealth:
    """Result of an integration health check."""
    status: str  # "healthy", "degraded", "unhealthy", "unauthorized"
    is_healthy: bool
    latency_ms: float = 0.0
    error_message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=_utc_now)


@dataclass
class ProviderMetadata:
    """Metadata describing an integration provider and its capabilities."""
    provider_id: str
    name: str
    description: str
    auth_type: AuthType
    required_scopes: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    setup_guide: str = ""
    doc_url: str = ""


class BaseConnector(ABC):
    """Abstract base class for all external service connectors."""

    @property
    @abstractmethod
    def metadata(self) -> ProviderMetadata:
        """Provider identification and capability metadata."""
        pass

    @abstractmethod
    async def test_connection(self, credentials: dict[str, Any]) -> ConnectionHealth:
        """Verify credential validity and service reachability."""
        pass

    @abstractmethod
    async def get_tools(self, user_id: int, credentials: dict[str, Any]) -> list[BaseTool]:
        """Expose executable capabilities as BaseTool instances."""
        pass

    async def refresh_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        """Refresh expired credentials (for OAuth2). Default is no-op for API keys."""
        return credentials

    async def revoke_credentials(self, credentials: dict[str, Any]) -> bool:
        """Revoke tokens or access with provider upon disconnection."""
        return True

