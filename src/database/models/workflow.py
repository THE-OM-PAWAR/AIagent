"""SQLAlchemy models for persistent multi-step agent workflows, steps, runs, and approval requests."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WorkflowState(str, Enum):
    """Formal workflow execution state machine values."""

    DRAFT = "draft"
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(str, Enum):
    """Lifecycle status of a human-in-the-loop workflow approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# Allowed state transition matrix: source_state -> set of valid target states
VALID_WORKFLOW_STATE_TRANSITIONS: dict[str, set[str]] = {
    WorkflowState.DRAFT.value: {WorkflowState.PENDING.value, WorkflowState.CANCELLED.value},
    WorkflowState.PENDING.value: {
        WorkflowState.RUNNING.value,
        WorkflowState.PAUSED.value,
        WorkflowState.CANCELLED.value,
    },
    WorkflowState.RUNNING.value: {
        WorkflowState.WAITING_FOR_APPROVAL.value,
        WorkflowState.PAUSED.value,
        WorkflowState.COMPLETED.value,
        WorkflowState.FAILED.value,
        WorkflowState.CANCELLED.value,
    },
    WorkflowState.WAITING_FOR_APPROVAL.value: {
        WorkflowState.RUNNING.value,
        WorkflowState.FAILED.value,
        WorkflowState.PAUSED.value,
        WorkflowState.CANCELLED.value,
    },
    WorkflowState.PAUSED.value: {
        WorkflowState.RUNNING.value,
        WorkflowState.PENDING.value,
        WorkflowState.CANCELLED.value,
    },
    WorkflowState.FAILED.value: {WorkflowState.RUNNING.value, WorkflowState.PENDING.value, WorkflowState.CANCELLED.value},
    WorkflowState.COMPLETED.value: set(),  # Terminal state
    WorkflowState.CANCELLED.value: set(),  # Terminal state
}


def validate_workflow_state_transition(current_state: str, new_state: str) -> None:
    """Validate whether transitioning from current_state to new_state is allowed.

    Raises:
        ValueError: If current_state is unknown or transition is forbidden.
    """
    curr = current_state.lower().strip()
    target = new_state.lower().strip()

    if curr == target:
        return  # Idempotent re-entry to the same state is permissible

    allowed_targets = VALID_WORKFLOW_STATE_TRANSITIONS.get(curr)
    if allowed_targets is None:
        raise ValueError(f"Unknown current workflow state '{current_state}'.")

    if target not in allowed_targets:
        raise ValueError(
            f"Invalid workflow state transition: cannot transition from '{curr}' to '{target}'. "
            f"Allowed next states: {sorted(allowed_targets) if allowed_targets else 'None (terminal state)'}."
        )


class WorkflowModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persistent workflow definition, status, context data, and execution checkpoint record."""

    __tablename__ = "workflows"

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_one_time: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Status: draft, pending, running, waiting_for_approval, paused, completed, failed, cancelled
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False, index=True)

    current_step_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Maintained for fast read access and backwards compatibility
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    # Key-value context data shared across steps (checkpoint)
    context_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    # Audit history log of step executions and state transitions
    execution_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    # Checkpoint snapshot data for recovery
    checkpoint_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relational step definitions, execution runs, and approvals
    step_definitions: Mapped[list["WorkflowStepModel"]] = relationship(
        "WorkflowStepModel",
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowStepModel.step_order",
        lazy="selectin",
    )
    runs: Mapped[list["WorkflowRunModel"]] = relationship(
        "WorkflowRunModel",
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="desc(WorkflowRunModel.created_at)",
        lazy="selectin",
    )
    approvals: Mapped[list["WorkflowApprovalModel"]] = relationship(
        "WorkflowApprovalModel",
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="desc(WorkflowApprovalModel.created_at)",
        lazy="selectin",
    )
    schedules: Mapped[list["WorkflowScheduleModel"]] = relationship(
        "WorkflowScheduleModel",
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="desc(WorkflowScheduleModel.created_at)",
        lazy="selectin",
    )


class WorkflowStepModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Normalized persistent workflow step definition with dependencies and condition rules."""

    __tablename__ = "workflow_steps"

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Input parameters template with optional {placeholder} substitutions
    input_template: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    # Explicit list of prerequisite step names/keys: ["step_1", "step_2"]
    dependencies: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    # Conditional execution rule: {"field": "step_1.count", "operator": "gt", "value": 0}
    condition: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    max_retries: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)

    # Step status: pending, running, completed, skipped, failed
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False, index=True)
    output_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    workflow: Mapped["WorkflowModel"] = relationship("WorkflowModel", back_populates="step_definitions")


class WorkflowRunModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Isolated runtime instance representing a specific execution pass of a workflow."""

    __tablename__ = "workflow_runs"

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    schedule_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("workflow_schedules.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Status: pending, running, waiting_for_approval, paused, completed, failed, cancelled
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False, index=True)
    trigger_type: Mapped[str] = mapped_column(String(50), default="manual", nullable=False)

    # Isolated execution context store specifically for this run
    context_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    current_step_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Audit records of step executions in this run
    step_runs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    # Concurrency control & Lease management
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow: Mapped[WorkflowModel] = relationship("WorkflowModel", back_populates="runs")
    schedule: Mapped[WorkflowScheduleModel | None] = relationship("WorkflowScheduleModel", back_populates="runs")
    approvals: Mapped[list[WorkflowApprovalModel]] = relationship(
        "WorkflowApprovalModel",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="desc(WorkflowApprovalModel.created_at)",
        lazy="selectin",
    )


class WorkflowApprovalModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persistent approval record required for consequential or irreversible external operations."""

    __tablename__ = "workflow_approvals"

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)

    action_summary: Mapped[str] = mapped_column(Text, nullable=False)
    action_arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    requested_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # Status: pending, approved, rejected, expired
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False, index=True)

    resolved_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    workflow: Mapped["WorkflowModel"] = relationship("WorkflowModel", back_populates="approvals")
    run: Mapped["WorkflowRunModel"] = relationship("WorkflowRunModel", back_populates="approvals")


class ScheduleStatus(str, Enum):
    """Lifecycle status of a persistent workflow schedule."""

    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class CatchUpPolicy(str, Enum):
    """Policy defining how missed executions during server downtime or restarts are handled."""

    RUN_ONCE = "run_once"
    SKIP_ALL = "skip_all"
    RUN_ALL = "run_all"


class WorkflowScheduleModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persistent trigger and recurrence schedule definition for a workflow."""

    __tablename__ = "workflow_schedules"

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Trigger type: one_time, interval, daily, weekly, cron, event
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False)
    trigger_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    # Status: active, paused, cancelled, completed
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False, index=True)

    # Catch-up policy: run_once, skip_all, run_all
    catch_up_policy: Mapped[str] = mapped_column(String(50), default="run_once", nullable=False)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    total_runs_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Distributed claiming
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Execution policies & Lifecycle controls
    allow_overlap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_consecutive_failures: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_call_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    workflow: Mapped[WorkflowModel] = relationship("WorkflowModel", back_populates="schedules")
    runs: Mapped[list[WorkflowRunModel]] = relationship(
        "WorkflowRunModel",
        back_populates="schedule",
        cascade="all, delete-orphan",
        order_by="desc(WorkflowRunModel.created_at)",
        lazy="selectin",
    )

