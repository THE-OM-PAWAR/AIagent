"""Authenticated encryption for credentials stored at rest."""

import base64
import hashlib
import json
import logging
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from src.integrations.errors import AuthenticationError

logger = logging.getLogger(__name__)


def derive_fernet_key(secret: str) -> bytes:
    """Derive a valid 32-byte URL-safe base64 Fernet key from any passphrase string."""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class CredentialEncryptor:
    """Encrypts and decrypts integration credentials at rest using Fernet authenticated cryptography."""

    def __init__(self, key: str | bytes | None = None) -> None:
        if key is None:
            raw_key = os.getenv("INTEGRATION_ENCRYPTION_KEY")
            if not raw_key:
                # Fallback to a stable derived key based on TELEGRAM_BOT_TOKEN or GEMINI_API_KEY
                seed = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("GEMINI_API_KEY") or "aiagent_default_secret_seed_key_2026"
                self._key = derive_fernet_key(seed)
            else:
                try:
                    # Check if already a valid 32-byte base64 Fernet key
                    decoded = base64.urlsafe_b64decode(raw_key.encode("utf-8"))
                    if len(decoded) == 32:
                        self._key = raw_key.encode("utf-8")
                    else:
                        self._key = derive_fernet_key(raw_key)
                except Exception:
                    self._key = derive_fernet_key(raw_key)
        elif isinstance(key, str):
            try:
                decoded = base64.urlsafe_b64decode(key.encode("utf-8"))
                if len(decoded) == 32:
                    self._key = key.encode("utf-8")
                else:
                    self._key = derive_fernet_key(key)
            except Exception:
                self._key = derive_fernet_key(key)
        else:
            self._key = key

        self._fernet = Fernet(self._key)

    def encrypt(self, data: dict[str, Any]) -> str:
        """Serialize and encrypt credential dictionary into an authenticated ciphertext string."""
        serialized = json.dumps(data, sort_keys=True)
        encrypted_bytes = self._fernet.encrypt(serialized.encode("utf-8"))
        return encrypted_bytes.decode("utf-8")

    def decrypt(self, ciphertext: str) -> dict[str, Any]:
        """Decrypt ciphertext and return the original credential dictionary."""
        try:
            decrypted_bytes = self._fernet.decrypt(ciphertext.encode("utf-8"))
            return json.loads(decrypted_bytes.decode("utf-8"))
        except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
            logger.error("Failed to decrypt credentials: invalid token or corrupted data.")
            raise AuthenticationError("Could not decrypt integration credentials. Key may have changed.") from exc


def mask_secret(secret: str | None) -> str:
    """Mask sensitive tokens or API keys for safe display in logs and UI."""
    if not secret:
        return ""
    s = str(secret).strip()
    if len(s) <= 4:
        return "****"
    if len(s) <= 8:
        return f"{s[:2]}****{s[-2:]}"
    # If key has a known provider prefix with hyphen (e.g., tvly-...)
    if "-" in s[:6]:
        idx = s.index("-")
        prefix = s[: idx + 1]
        suffix = s[-4:]
        return f"{prefix}****{suffix}"
    return f"{s[:4]}****{s[-4:]}"
