"""Modular integration framework for external services and search providers."""

from src.integrations.base import AuthType, BaseConnector, ConnectionHealth, ProviderMetadata
from src.integrations.connectors.web_search import WebSearchConnector, WebSearchTool
from src.integrations.errors import (
    AuthenticationError,
    ContentSanitizationError,
    CredentialNotFoundError,
    IntegrationError,
    ProviderUnavailableError,
    RateLimitError,
    ScopeError,
)
from src.integrations.registry import IntegrationRegistry
from src.integrations.security import CredentialEncryptor, OAuthStateSigner, derive_fernet_key, mask_secret

__all__ = [
    "AuthType",
    "BaseConnector",
    "ConnectionHealth",
    "ProviderMetadata",
    "IntegrationRegistry",
    "CredentialEncryptor",
    "OAuthStateSigner",
    "derive_fernet_key",
    "mask_secret",
    "WebSearchConnector",
    "WebSearchTool",
    "IntegrationError",
    "AuthenticationError",
    "CredentialNotFoundError",
    "RateLimitError",
    "ProviderUnavailableError",
    "ScopeError",
    "ContentSanitizationError",
]

