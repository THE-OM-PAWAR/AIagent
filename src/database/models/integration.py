"""SQLAlchemy models for integration credentials and security audit logs."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class IntegrationCredentialModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Encrypted credential storage for external integration providers."""

    __tablename__ = "integration_credentials"

    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), default="api_key", nullable=False)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="connected", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "provider_id", name="uq_user_provider_credentials"),
        Index("ix_user_provider_status", "user_id", "provider_id", "status"),
    )


class IntegrationAuditLogModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Audit log for integration lifecycle events (never contains credentials)."""

    __tablename__ = "integration_audit_logs"

    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_audit_user_provider_time", "user_id", "provider_id", "created_at"),
    )

