"""Calendly OAuth2 client for authorization, token exchange, refresh, and revocation."""

import logging
import time
from typing import Any
import urllib.parse

import httpx

from src.integrations.base import ConnectionHealth
from src.integrations.errors import (
    AuthenticationError,
    ProviderUnavailableError,
)
from src.integrations.http_client import IntegrationHttpClient
from src.integrations.security.oauth import OAuthStateSigner

logger = logging.getLogger(__name__)

CALENDLY_AUTH_ENDPOINT = "https://auth.calendly.com/oauth/authorize"
CALENDLY_TOKEN_ENDPOINT = "https://auth.calendly.com/oauth/token"
CALENDLY_REVOKE_ENDPOINT = "https://auth.calendly.com/oauth/revoke"
CALENDLY_API_BASE = "https://api.calendly.com"


class CalendlyOAuthClient:
    """Manages Calendly OAuth2 authorization, token exchange, refresh, and revocation."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob",
        state_signer: OAuthStateSigner | None = None,
        http_client: IntegrationHttpClient | None = None,
    ) -> None:
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self.redirect_uri = redirect_uri
        self.state_signer = state_signer or OAuthStateSigner()
        self.http_client = http_client or IntegrationHttpClient(provider_id="calendly_oauth")

    def generate_authorization_url(self, user_id: int) -> tuple[str, str]:
        """Generate a signed Calendly OAuth authorization URL.

        Returns:
            Tuple of (authorization_url, csrf_token).
        """
        if not self.client_id:
            raise AuthenticationError(
                "CALENDLY_CLIENT_ID is not configured. Set CALENDLY_CLIENT_ID and CALENDLY_CLIENT_SECRET or use a Personal Access Token.",
                provider_id="calendly",
            )

        state_token, csrf_token = self.state_signer.generate_state(
            user_id=user_id,
            provider_id="calendly",
            redirect_uri=self.redirect_uri,
        )

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "state": state_token,
        }
        url = f"{CALENDLY_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"
        return url, csrf_token

    async def exchange_code(
        self,
        code: str,
        redirect_uri: str | None = None,
    ) -> dict[str, Any]:
        """Exchange authorization code for access and refresh tokens."""
        if not self.client_id or not self.client_secret:
            raise AuthenticationError(
                "Calendly client ID or client secret missing.",
                provider_id="calendly",
            )

        payload = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code.strip(),
            "redirect_uri": redirect_uri or self.redirect_uri,
        }

        resp = await self.http_client.post(CALENDLY_TOKEN_ENDPOINT, data=payload, idempotent=False)
        data = resp.json()
        expires_in = int(data.get("expires_in", 7200))
        expires_at = int(time.time()) + expires_in

        return {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "token_type": data.get("token_type", "Bearer"),
            "expires_in": expires_in,
            "expires_at": expires_at,
            "owner": data.get("owner"),
            "organization": data.get("organization"),
        }

    async def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Renew expired Calendly access token using refresh_token."""
        if not refresh_token:
            raise AuthenticationError("Refresh token missing.", provider_id="calendly")

        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token.strip(),
        }

        resp = await self.http_client.post(CALENDLY_TOKEN_ENDPOINT, data=payload, idempotent=True)
        data = resp.json()
        expires_in = int(data.get("expires_in", 7200))
        expires_at = int(time.time()) + expires_in

        return {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token") or refresh_token,
            "token_type": data.get("token_type", "Bearer"),
            "expires_in": expires_in,
            "expires_at": expires_at,
            "owner": data.get("owner"),
            "organization": data.get("organization"),
        }

    async def revoke_token(self, token: str) -> bool:
        """Revoke a token with Calendly."""
        if not token:
            return True

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "token": token.strip(),
        }
        try:
            resp = await self.http_client.post(CALENDLY_REVOKE_ENDPOINT, data=payload, idempotent=True)
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("Error revoking Calendly token: %s", exc)
            return False

    async def test_connection(self, credentials: dict[str, Any]) -> ConnectionHealth:
        """Test validity of token by probing Calendly /users/me."""
        token = credentials.get("access_token") or credentials.get("api_key")
        if not token:
            return ConnectionHealth(status="unauthorized", is_healthy=False, error_message="No token or API key provided.")

        start = time.perf_counter()
        headers = {"Authorization": f"Bearer {token.strip()}"}

        try:
            resp = await self.http_client.get(f"{CALENDLY_API_BASE}/users/me", headers=headers)
            latency = round((time.perf_counter() - start) * 1000.0, 1)

            if resp.status_code == 200:
                user_res = resp.json().get("resource", {})
                email = user_res.get("email", "unknown")
                name = user_res.get("name", "Calendly User")
                return ConnectionHealth(
                    status="healthy",
                    is_healthy=True,
                    latency_ms=latency,
                    details={"email": email, "name": name, "scheduling_url": user_res.get("scheduling_url")},
                )
            return ConnectionHealth(status="degraded", is_healthy=False, latency_ms=latency, error_message=f"HTTP {resp.status_code}")
        except AuthenticationError as auth_err:
            latency = round((time.perf_counter() - start) * 1000.0, 1)
            return ConnectionHealth(status="unauthorized", is_healthy=False, latency_ms=latency, error_message=str(auth_err))
        except Exception as exc:
            latency = round((time.perf_counter() - start) * 1000.0, 1)
            return ConnectionHealth(status="unhealthy", is_healthy=False, latency_ms=latency, error_message=f"Network error: {exc}")

