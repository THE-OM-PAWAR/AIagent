"""SQLAlchemy model for persistent multi-step agent workflows and checkpoints."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WorkflowModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persistent workflow definition, status, context data, and execution checkpoint record."""

    __tablename__ = "workflows"

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Status: pending, running, waiting_for_approval, paused, completed, failed, cancelled
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False, index=True)

    current_step_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # List of step definitions: [{"name": "step1", "tool_name": "calc", "arguments": {...}, "max_retries": 2}]
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    # Key-value context data shared across steps
    context_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    # Audit history log of step executions
    execution_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    # Checkpoint snapshot data for recovery
    checkpoint_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

