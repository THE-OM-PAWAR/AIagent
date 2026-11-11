"""Centralized registry for integration connectors, health checks, and tool discovery."""

from datetime import datetime, timezone
import json
import logging
from typing import Any

from src.database.repositories.integration_repo import IntegrationRepository
from src.integrations.base import AuthType, BaseConnector, ConnectionHealth, ProviderMetadata
from src.integrations.errors import (
    AuthenticationError,
    CredentialNotFoundError,
    IntegrationError,
    ProviderUnavailableError,
)
from src.integrations.security.crypto import CredentialEncryptor, mask_secret
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class IntegrationRegistry:
    """Manages connector registration, credential encryption, and tool export."""

    def __init__(
        self,
        encryptor: CredentialEncryptor | None = None,
    ) -> None:
        self._connectors: dict[str, BaseConnector] = {}
        self.encryptor = encryptor or CredentialEncryptor()

    def register_connector(self, connector: BaseConnector) -> None:
        """Register an integration connector."""
        pid = connector.metadata.provider_id
        self._connectors[pid] = connector
        logger.info("Registered integration connector: %s (%s)", connector.metadata.name, pid)

    def get_connector(self, provider_id: str) -> BaseConnector | None:
        """Get a registered connector by provider ID."""
        return self._connectors.get(provider_id)

    def list_providers(self) -> list[ProviderMetadata]:
        """List metadata for all registered integration providers."""
        return [c.metadata for c in self._connectors.values()]

    async def connect_api_key(
        self,
        repo: IntegrationRepository,
        user_id: int,
        provider_id: str,
        api_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> ConnectionHealth:
        """Validate, encrypt, and store API-key credentials for a user."""
        connector = self.get_connector(provider_id)
        if not connector:
            raise CredentialNotFoundError(f"Provider '{provider_id}' is not supported.", provider_id=provider_id)

        creds = {"api_key": api_key.strip()}
        if metadata:
            creds.update(metadata)

        # 1. Test live connection before persisting
        health = await connector.test_connection(creds)
        if not health.is_healthy:
            await repo.log_audit(
                user_id=user_id,
                provider_id=provider_id,
                action="connect_attempt",
                status="failed",
                details={"error": health.error_message},
            )
            return health

        # 2. Encrypt credentials at rest
        encrypted = self.encryptor.encrypt(creds)

        # 3. Save to repository
        safe_meta = {
            "masked_key": mask_secret(api_key),
            "connected_at": datetime.now(timezone.utc).isoformat(),
        }
        await repo.save_credentials(
            user_id=user_id,
            provider_id=provider_id,
            encrypted_credentials=encrypted,
            auth_type=connector.metadata.auth_type.value,
            scopes=",".join(connector.metadata.required_scopes),
            metadata_json=json.dumps(safe_meta),
        )

        # 4. Audit log (never exposes credentials)
        await repo.log_audit(
            user_id=user_id,
            provider_id=provider_id,
            action="connect",
            status="success",
            details={"masked_key": mask_secret(api_key)},
        )

        logger.info("User %s successfully connected integration '%s'", user_id, provider_id)
        return health

    async def connect_oauth_tokens(
        self,
        repo: IntegrationRepository,
        user_id: int,
        provider_id: str,
        token_data: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> ConnectionHealth:
        """Validate, encrypt, and store OAuth2 tokens for a user."""
        connector = self.get_connector(provider_id)
        if not connector:
            raise CredentialNotFoundError(f"Provider '{provider_id}' is not supported.", provider_id=provider_id)

        # 1. Test live connection before persisting
        health = await connector.test_connection(token_data)
        if not health.is_healthy:
            await repo.log_audit(
                user_id=user_id,
                provider_id=provider_id,
                action="oauth_connect_attempt",
                status="failed",
                details={"error": health.error_message},
            )
            return health

        # 2. Encrypt credentials at rest
        encrypted = self.encryptor.encrypt(token_data)

        # 3. Store safe metadata
        safe_meta = {
            "masked_key": mask_secret(token_data.get("access_token")),
            "scope": token_data.get("scope", ""),
            "connected_at": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            safe_meta.update(metadata)

        scopes_str = token_data.get("scope") or ",".join(connector.metadata.required_scopes)
        await repo.save_credentials(
            user_id=user_id,
            provider_id=provider_id,
            encrypted_credentials=encrypted,
            auth_type=AuthType.OAUTH2.value,
            scopes=scopes_str,
            metadata_json=json.dumps(safe_meta),
        )

        # 4. Audit log
        await repo.log_audit(
            user_id=user_id,
            provider_id=provider_id,
            action="oauth_connect",
            status="success",
            details={"scopes": scopes_str, "masked_key": safe_meta["masked_key"]},
        )
        logger.info("User %s successfully connected OAuth integration '%s'", user_id, provider_id)
        return health

    async def disconnect(
        self,
        repo: IntegrationRepository,
        user_id: int,
        provider_id: str,
    ) -> bool:
        """Revoke tokens with provider and delete credentials for user."""
        connector = self.get_connector(provider_id)
        creds = await self.get_credentials(repo, user_id, provider_id)
        if creds and connector:
            try:
                await connector.revoke_credentials(creds)
            except Exception as rev_err:
                logger.warning("Token revocation failed with provider %s: %s", provider_id, rev_err)

        deleted = await repo.delete_credentials(user_id, provider_id)
        if provider_id == "google":
            for sub_p in ("gmail", "google_calendar", "google_drive"):
                await repo.delete_credentials(user_id, sub_p)

        if deleted:
            await repo.log_audit(
                user_id=user_id,
                provider_id=provider_id,
                action="disconnect",
                status="success",
                details={"revoked": True},
            )
            logger.info("User %s disconnected integration '%s'", user_id, provider_id)
        return deleted

    async def get_credentials(
        self,
        repo: IntegrationRepository,
        user_id: int,
        provider_id: str,
    ) -> dict[str, Any] | None:
        """Retrieve, auto-refresh if expiring, and decrypt credentials for an integration."""
        rec = await repo.get_credentials(user_id, provider_id)
        effective_provider_id = provider_id
        if (not rec or rec.status != "connected") and provider_id in ("gmail", "google_calendar", "google_drive"):
            rec = await repo.get_credentials(user_id, "google")
            effective_provider_id = "google"

        if not rec or rec.status != "connected":
            return None

        try:
            creds = self.encryptor.decrypt(rec.encrypted_credentials)
        except Exception as exc:
            logger.error("Failed to decrypt credentials for user %s, provider %s: %s", user_id, provider_id, exc)
            await repo.update_status(user_id, effective_provider_id, "error", error_message="Credential decryption failed")
            return None

        # Auto-refresh expired OAuth2 tokens if expiring in less than 60 seconds
        expires_at = creds.get("expires_at")
        if expires_at and datetime.now(timezone.utc).timestamp() > (float(expires_at) - 60):
            connector = self.get_connector(effective_provider_id) or self.get_connector(provider_id)
            if connector:
                try:
                    refreshed = await connector.refresh_credentials(creds)
                    if refreshed and refreshed != creds:
                        creds = refreshed
                        new_enc = self.encryptor.encrypt(creds)
                        await repo.save_credentials(
                            user_id=user_id,
                            provider_id=effective_provider_id,
                            encrypted_credentials=new_enc,
                            auth_type=rec.auth_type,
                            scopes=rec.scopes,
                            metadata_json=rec.metadata_json,
                        )
                        logger.info("Auto-refreshed OAuth token for user %s, provider %s", user_id, effective_provider_id)
                except Exception as ref_err:
                    logger.warning("Auto-refresh failed for user %s, provider %s: %s", user_id, effective_provider_id, ref_err)

        return creds

    async def check_health(
        self,
        repo: IntegrationRepository,
        user_id: int,
        provider_id: str,
    ) -> ConnectionHealth:
        """Run health check against stored credentials for user."""
        connector = self.get_connector(provider_id)
        if not connector:
            return ConnectionHealth(status="unhealthy", is_healthy=False, error_message=f"Unknown provider '{provider_id}'")

        creds = await self.get_credentials(repo, user_id, provider_id)
        if not creds:
            return ConnectionHealth(status="not_connected", is_healthy=False, error_message="Integration not connected.")

        health = await connector.test_connection(creds)
        new_status = "connected" if health.is_healthy else "degraded"
        await repo.update_status(user_id, provider_id, new_status, error_message=health.error_message)

        await repo.log_audit(
            user_id=user_id,
            provider_id=provider_id,
            action="health_check",
            status="success" if health.is_healthy else "failed",
            details={"is_healthy": health.is_healthy, "latency_ms": health.latency_ms},
        )
        return health

    async def get_status(
        self,
        repo: IntegrationRepository,
        user_id: int,
    ) -> list[dict[str, Any]]:
        """Return comprehensive status for all registered integrations for user."""
        user_creds = {rec.provider_id: rec for rec in await repo.list_user_integrations(user_id)}
        results: list[dict[str, Any]] = []

        for meta in self.list_providers():
            rec = user_creds.get(meta.provider_id)
            is_connected = rec is not None and rec.status == "connected"
            masked_key = None
            if rec and rec.metadata_json:
                try:
                    data = json.loads(rec.metadata_json)
                    masked_key = data.get("masked_key")
                except Exception:
                    pass

            results.append({
                "provider_id": meta.provider_id,
                "name": meta.name,
                "description": meta.description,
                "auth_type": meta.auth_type.value,
                "capabilities": meta.capabilities,
                "is_connected": is_connected,
                "status": rec.status if rec else "disconnected",
                "last_verified_at": rec.last_verified_at.isoformat() if rec and rec.last_verified_at else None,
                "error_message": rec.error_message if rec else None,
                "masked_key": masked_key,
            })

        return results

    async def get_user_tools(
        self,
        repo: IntegrationRepository,
        user_id: int,
    ) -> list[BaseTool]:
        """Discover and return all active BaseTools from user's connected integrations."""
        tools: list[BaseTool] = []
        for pid, connector in self._connectors.items():
            creds = await self.get_credentials(repo, user_id, pid)
            if creds:
                try:
                    conn_tools = await connector.get_tools(user_id, creds)
                    tools.extend(conn_tools)
                except Exception as exc:
                    logger.error("Failed loading tools for provider %s: %s", pid, exc)
        return tools

