"""Shared Google OAuth2 client for Gmail, Google Calendar, and Google Drive."""

from datetime import datetime, timezone
import logging
import time
from typing import Any
import urllib.parse

import httpx

from src.integrations.base import ConnectionHealth
from src.integrations.errors import (
    AuthenticationError,
    IntegrationError,
    ProviderUnavailableError,
)
from src.integrations.security.oauth import OAuthStateSigner

logger = logging.getLogger(__name__)

# Standard Google OAuth 2.0 endpoints
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"

# Least-privilege Google Workspace scopes
GOOGLE_WORKSPACE_SCOPES = [
    # Gmail
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    # Google Calendar
    "https://www.googleapis.com/auth/calendar.events",
    # Google Drive
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    # User identity for verification
    "https://www.googleapis.com/auth/userinfo.email",
]


class GoogleOAuthClient:
    """Manages Google OAuth2 authorization flows, tokens, refresh, and revocation."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob",
        scopes: list[str] | None = None,
        state_signer: OAuthStateSigner | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self.redirect_uri = redirect_uri
        self.scopes = scopes or GOOGLE_WORKSPACE_SCOPES
        self.state_signer = state_signer or OAuthStateSigner()
        self.timeout = timeout

    def generate_authorization_url(self, user_id: int) -> tuple[str, str]:
        """Generate a Google OAuth2 authorization URL with signed state parameter.

        Returns:
            Tuple of (authorization_url, csrf_token).
        """
        if not self.client_id:
            raise AuthenticationError(
                "GOOGLE_CLIENT_ID is not configured. Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
                provider_id="google",
            )

        state_token, csrf_token = self.state_signer.generate_state(
            user_id=user_id,
            provider_id="google",
            redirect_uri=self.redirect_uri,
        )

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "prompt": "consent",
            "state": state_token,
        }

        url = f"{GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"
        return url, csrf_token

    async def exchange_code(
        self,
        code: str,
        redirect_uri: str | None = None,
    ) -> dict[str, Any]:
        """Exchange authorization code for access and refresh tokens."""
        if not self.client_id or not self.client_secret:
            raise AuthenticationError(
                "Google OAuth credentials missing. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
                provider_id="google",
            )

        clean_code = code.strip()
        payload = {
            "code": clean_code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri or self.redirect_uri,
            "grant_type": "authorization_code",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(GOOGLE_TOKEN_ENDPOINT, data=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    expires_in = int(data.get("expires_in", 3600))
                    expires_at = int(time.time()) + expires_in
                    return {
                        "access_token": data.get("access_token"),
                        "refresh_token": data.get("refresh_token"),
                        "token_type": data.get("token_type", "Bearer"),
                        "expires_in": expires_in,
                        "expires_at": expires_at,
                        "scope": data.get("scope", " ".join(self.scopes)),
                    }

                if resp.status_code in (400, 401):
                    err_desc = resp.json().get("error_description", resp.text)
                    logger.warning("Google OAuth exchange failed: %s", err_desc)
                    raise AuthenticationError(f"Google authorization failed: {err_desc}", provider_id="google")

                raise ProviderUnavailableError(
                    f"Google token endpoint returned status {resp.status_code}.",
                    provider_id="google",
                )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError("Google OAuth server timed out.", provider_id="google") from exc

    async def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Use refresh_token to acquire a fresh access_token."""
        if not refresh_token:
            raise AuthenticationError("Refresh token missing.", provider_id="google")

        payload = {
            "refresh_token": refresh_token.strip(),
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(GOOGLE_TOKEN_ENDPOINT, data=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    expires_in = int(data.get("expires_in", 3600))
                    expires_at = int(time.time()) + expires_in
                    return {
                        "access_token": data.get("access_token"),
                        "refresh_token": data.get("refresh_token") or refresh_token,
                        "token_type": data.get("token_type", "Bearer"),
                        "expires_in": expires_in,
                        "expires_at": expires_at,
                        "scope": data.get("scope", " ".join(self.scopes)),
                    }

                if resp.status_code in (400, 401):
                    err_desc = resp.json().get("error_description", resp.text)
                    raise AuthenticationError(f"Failed to refresh Google token: {err_desc}", provider_id="google")

                raise ProviderUnavailableError(f"Google token refresh returned status {resp.status_code}.", provider_id="google")
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError("Google token refresh timed out.", provider_id="google") from exc

    async def revoke_token(self, token: str) -> bool:
        """Revoke an access or refresh token with Google."""
        if not token:
            return True

        params = {"token": token.strip()}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(GOOGLE_REVOKE_ENDPOINT, params=params)
                if resp.status_code == 200:
                    logger.info("Google OAuth token successfully revoked.")
                    return True
                logger.warning("Google revoke returned status %s: %s", resp.status_code, resp.text)
                return False
        except Exception as exc:
            logger.warning("Error revoking Google token: %s", exc)
            return False

    async def test_connection(self, credentials: dict[str, Any]) -> ConnectionHealth:
        """Verify token validity by probing Google UserInfo API."""
        access_token = credentials.get("access_token")
        if not access_token:
            return ConnectionHealth(status="unauthorized", is_healthy=False, error_message="Access token is empty.")

        start = time.perf_counter()
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(GOOGLE_USERINFO_ENDPOINT, headers=headers)
                latency = round((time.perf_counter() - start) * 1000.0, 1)

                if resp.status_code == 200:
                    data = resp.json()
                    email = data.get("email", "connected")
                    return ConnectionHealth(
                        status="healthy",
                        is_healthy=True,
                        latency_ms=latency,
                        details={"email": email},
                    )

                if resp.status_code in (401, 403):
                    # Check if refresh is possible
                    refresh_token = credentials.get("refresh_token")
                    if refresh_token:
                        try:
                            new_creds = await self.refresh_access_token(refresh_token)
                            credentials.update(new_creds)
                            return await self.test_connection(credentials)
                        except Exception:
                            pass
                    return ConnectionHealth(
                        status="unauthorized",
                        is_healthy=False,
                        latency_ms=latency,
                        error_message="Google token has expired or was revoked.",
                    )

                return ConnectionHealth(
                    status="degraded",
                    is_healthy=False,
                    latency_ms=latency,
                    error_message=f"Google returned status {resp.status_code}.",
                )
        except Exception as exc:
            latency = round((time.perf_counter() - start) * 1000.0, 1)
            return ConnectionHealth(
                status="unhealthy",
                is_healthy=False,
                latency_ms=latency,
                error_message=f"Network error verifying Google account: {exc}",
            )

