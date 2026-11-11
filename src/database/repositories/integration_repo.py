"""Repository for integration credentials and security audit logs."""

from datetime import datetime, timezone
import json
import logging
from typing import Any

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models.integration import IntegrationAuditLogModel, IntegrationCredentialModel

logger = logging.getLogger(__name__)


class IntegrationRepository:
    """Async repository managing encrypted credentials and audit events in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save_credentials(
        self,
        user_id: int,
        provider_id: str,
        encrypted_credentials: str,
        auth_type: str = "api_key",
        scopes: str | None = None,
        expires_at: datetime | None = None,
        metadata_json: str | None = None,
    ) -> IntegrationCredentialModel:
        """Upsert integration credentials for a specific user and provider."""
        stmt = select(IntegrationCredentialModel).where(
            IntegrationCredentialModel.user_id == user_id,
            IntegrationCredentialModel.provider_id == provider_id,
        )
        res = await self.session.execute(stmt)
        record = res.scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if record:
            record.encrypted_credentials = encrypted_credentials
            record.auth_type = auth_type
            record.scopes = scopes
            record.status = "connected"
            record.expires_at = expires_at
            record.last_verified_at = now
            record.error_message = None
            if metadata_json:
                record.metadata_json = metadata_json
            record.updated_at = now
        else:
            record = IntegrationCredentialModel(
                user_id=user_id,
                provider_id=provider_id,
                auth_type=auth_type,
                encrypted_credentials=encrypted_credentials,
                scopes=scopes,
                status="connected",
                expires_at=expires_at,
                last_verified_at=now,
                error_message=None,
                metadata_json=metadata_json,
            )
            self.session.add(record)

        await self.session.flush()
        return record

    async def get_credentials(
        self,
        user_id: int,
        provider_id: str,
    ) -> IntegrationCredentialModel | None:
        """Retrieve stored credential record for a given user and provider."""
        stmt = select(IntegrationCredentialModel).where(
            IntegrationCredentialModel.user_id == user_id,
            IntegrationCredentialModel.provider_id == provider_id,
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def delete_credentials(
        self,
        user_id: int,
        provider_id: str,
    ) -> bool:
        """Revoke and delete credentials for a user and provider."""
        stmt = delete(IntegrationCredentialModel).where(
            IntegrationCredentialModel.user_id == user_id,
            IntegrationCredentialModel.provider_id == provider_id,
        )
        res = await self.session.execute(stmt)
        await self.session.flush()
        return (res.rowcount or 0) > 0

    async def list_user_integrations(
        self,
        user_id: int,
    ) -> list[IntegrationCredentialModel]:
        """List all integration records for a user."""
        stmt = (
            select(IntegrationCredentialModel)
            .where(IntegrationCredentialModel.user_id == user_id)
            .order_by(IntegrationCredentialModel.provider_id)
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def update_status(
        self,
        user_id: int,
        provider_id: str,
        status: str,
        error_message: str | None = None,
    ) -> bool:
        """Update connection status and error message."""
        record = await self.get_credentials(user_id, provider_id)
        if not record:
            return False
        record.status = status
        record.error_message = error_message
        record.updated_at = datetime.now(timezone.utc)
        if status == "connected":
            record.last_verified_at = record.updated_at
        await self.session.flush()
        return True

    async def log_audit(
        self,
        user_id: int,
        provider_id: str,
        action: str,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> IntegrationAuditLogModel:
        """Record sanitized audit event for integration operations."""
        details_str = json.dumps(details) if details else None
        audit = IntegrationAuditLogModel(
            user_id=user_id,
            provider_id=provider_id,
            action=action,
            status=status,
            details_json=details_str,
        )
        self.session.add(audit)
        await self.session.flush()
        return audit

    async def get_recent_audit_logs(
        self,
        user_id: int,
        provider_id: str | None = None,
        limit: int = 20,
    ) -> list[IntegrationAuditLogModel]:
        """Retrieve recent audit logs for a user, optionally filtered by provider."""
        stmt = select(IntegrationAuditLogModel).where(IntegrationAuditLogModel.user_id == user_id)
        if provider_id:
            stmt = stmt.where(IntegrationAuditLogModel.provider_id == provider_id)
        stmt = stmt.order_by(desc(IntegrationAuditLogModel.created_at)).limit(limit)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    # Alias for method naming convenience
    list_audit_logs = get_recent_audit_logs
