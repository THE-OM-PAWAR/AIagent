"""Signed OAuth state management with CSRF protection and expiration."""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any

from src.integrations.errors import AuthenticationError

logger = logging.getLogger(__name__)


class OAuthStateSigner:
    """Generates and validates tamper-evident, expiring OAuth state parameters."""

    def __init__(self, secret_key: str | bytes | None = None) -> None:
        if secret_key is None:
            secret_key = "oauth_state_signing_key_secret_2026"
        self._secret_key = secret_key.encode("utf-8") if isinstance(secret_key, str) else secret_key

    def generate_state(
        self,
        user_id: int,
        provider_id: str,
        redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob",
        ttl_seconds: int = 600,
    ) -> tuple[str, str]:
        """Generate a signed state token and a matching CSRF verification token.

        Returns:
            Tuple of (state_token, csrf_token).
        """
        csrf_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + ttl_seconds

        payload = {
            "user_id": user_id,
            "provider_id": provider_id,
            "redirect_uri": redirect_uri,
            "expires_at": expires_at,
            "csrf_token": csrf_token,
        }

        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("utf-8")

        sig = hmac.new(self._secret_key, payload_bytes, hashlib.sha256).hexdigest()
        state_token = f"{payload_b64}.{sig}"

        return state_token, csrf_token

    def validate_state(
        self,
        state_token: str,
        expected_csrf: str | None = None,
    ) -> dict[str, Any]:
        """Validate state signature, expiration, and CSRF token.

        Raises:
            AuthenticationError: If signature is forged, token expired, or CSRF fails.
        """
        try:
            parts = state_token.split(".")
            if len(parts) != 2:
                raise AuthenticationError("Malformed OAuth state format.")

            payload_b64, signature = parts
            payload_bytes = base64.urlsafe_b64decode(payload_b64.encode("utf-8"))
            expected_sig = hmac.new(self._secret_key, payload_bytes, hashlib.sha256).hexdigest()

            if not hmac.compare_digest(signature, expected_sig):
                logger.warning("OAuth state signature mismatch: possible tampering.")
                raise AuthenticationError("Invalid OAuth state signature.")

            payload = json.loads(payload_bytes.decode("utf-8"))
        except AuthenticationError:
            raise
        except Exception as exc:
            logger.warning("Failed parsing OAuth state: %s", exc)
            raise AuthenticationError("Corrupted OAuth state token.") from exc

        # Check expiration
        expires_at = payload.get("expires_at", 0)
        if time.time() > expires_at:
            logger.warning("OAuth state expired at %s (current time: %s)", expires_at, int(time.time()))
            raise AuthenticationError("OAuth state has expired. Please initiate connection again.")

        # Check CSRF
        if expected_csrf is not None:
            state_csrf = payload.get("csrf_token", "")
            if not hmac.compare_digest(state_csrf, expected_csrf):
                logger.warning("OAuth CSRF token mismatch.")
                raise AuthenticationError("CSRF verification failed.")

        return payload

    def verify_state(
        self,
        state_token: str,
        expected_csrf: str | None = None,
        expected_csrf_token: str | None = None,
    ) -> dict[str, Any]:
        """Validate state signature, expiration, and CSRF token."""
        return self.validate_state(state_token, expected_csrf=expected_csrf or expected_csrf_token)
