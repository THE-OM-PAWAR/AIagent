"""SQLAlchemy models for reusable automation configurations and persistent automation findings."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AutomationType(str, Enum):
    """Supported core recurring automation use cases."""

    JOB_HUNTER = "job_hunter"
    CLIENT_ACQUISITION = "client_acquisition"
    CONTENT_ENGINE = "content_engine"
    NETWORKING_HUNTER = "networking_hunter"


class AutomationFindingStatus(str, Enum):
    """Lifecycle status for discovered opportunities, leads, content, and contacts."""

    DISCOVERED = "discovered"
    QUALIFIED = "qualified"
    DRAFT_PREPARED = "draft_prepared"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    CONTACTED = "contacted"
    APPLIED = "applied"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class AutomationConfigModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """User-configured automation template definition, schedule linkage, and preferences."""

    __tablename__ = "automation_configs"

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    automation_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Linkage to underlying workflow definition
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Linkage to recurring schedule
    schedule_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("workflow_schedules.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Structured user configuration: search queries, profile, filters, budgets, notification prefs
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    # Relational associations
    workflow: Mapped["WorkflowModel"] = relationship("WorkflowModel", lazy="selectin")
    schedule: Mapped["WorkflowScheduleModel | None"] = relationship("WorkflowScheduleModel", lazy="selectin")
    findings: Mapped[list["AutomationFindingModel"]] = relationship(
        "AutomationFindingModel",
        back_populates="automation_config",
        cascade="all, delete-orphan",
        order_by="desc(AutomationFindingModel.created_at)",
        lazy="selectin",
    )


class AutomationFindingModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persistent storage for qualified items, research findings, drafts, and follow-ups."""

    __tablename__ = "automation_findings"

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    automation_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    automation_config_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("automation_configs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Deterministic deduplication key (e.g., hash of URL, place_id, or normalized entity identifier)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    qualification_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Status: discovered, qualified, draft_prepared, contacted, applied, published, rejected, archived
    status: Mapped[str] = mapped_column(String(50), default="qualified", nullable=False, index=True)

    # Structured research findings (company details, requirements, verified contact info, etc.)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    # Generated customized outreach, proposal, content draft, or application cover letter
    generated_draft: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Follow-up scheduling
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Execution traceability linkages
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    automation_config: Mapped["AutomationConfigModel | None"] = relationship(
        "AutomationConfigModel", back_populates="findings"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "automation_type", "dedup_key", name="uq_user_automation_finding_dedup"),
    )

