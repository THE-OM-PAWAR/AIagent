"""Structured exceptions for external integrations."""

from typing import Any


class IntegrationError(Exception):
    """Base exception for all integration failures."""

    def __init__(self, message: str, provider_id: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.details = details or {}


class AuthenticationError(IntegrationError):
    """Raised when integration credentials, tokens, or API keys are invalid or expired."""
    pass


class CredentialNotFoundError(IntegrationError):
    """Raised when an operation requires an integration that is not connected."""
    pass


class RateLimitError(IntegrationError):
    """Raised when an external API rate limit is exceeded."""

    def __init__(
        self,
        message: str = "External provider rate limit exceeded.",
        provider_id: str | None = None,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, provider_id=provider_id, details=details)
        self.retry_after = retry_after


class ProviderUnavailableError(IntegrationError):
    """Raised when an external service is down or returning 5xx responses."""
    pass


class ScopeError(IntegrationError):
    """Raised when integration lacks required authorization scopes or permissions."""
    pass


class ContentSanitizationError(IntegrationError):
    """Raised when external content violates safety boundaries or contains malicious injection."""
    pass


class WebhookVerificationError(IntegrationError):
    """Raised when webhook signatures fail HMAC cryptographic verification."""
    pass


class CoordinationError(IntegrationError):
    """Raised when cross-service coordination or calendar reconciliation encounters an error."""
    pass

