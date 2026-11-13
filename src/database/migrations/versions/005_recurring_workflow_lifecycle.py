"""Recurring workflow lifecycle, execution policies, run schedule linkages, and notification status schema.

Revision ID: 005_recurring_workflow_lifecycle
Revises: 004_workflow_scheduler
Create Date: 2026-11-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005_recurring_workflow_lifecycle"
down_revision: Union[str, None] = "004_workflow_scheduler"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Update workflow_runs table
    run_cols = [c["name"] for c in inspector.get_columns("workflow_runs")]
    if "schedule_id" not in run_cols:
        op.add_column(
            "workflow_runs",
            sa.Column("schedule_id", sa.String(length=36), sa.ForeignKey("workflow_schedules.id", ondelete="SET NULL"), nullable=True),
        )
        op.create_index("idx_workflow_runs_schedule_id", "workflow_runs", ["schedule_id"])

    if "notification_sent" not in run_cols:
        op.add_column(
            "workflow_runs",
            sa.Column("notification_sent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )

    # 2. Update workflow_schedules table
    sch_cols = [c["name"] for c in inspector.get_columns("workflow_schedules")]
    if "allow_overlap" not in sch_cols:
        op.add_column(
            "workflow_schedules",
            sa.Column("allow_overlap", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
    if "max_concurrency" not in sch_cols:
        op.add_column(
            "workflow_schedules",
            sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )
    if "consecutive_failures" not in sch_cols:
        op.add_column(
            "workflow_schedules",
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
    if "max_consecutive_failures" not in sch_cols:
        op.add_column(
            "workflow_schedules",
            sa.Column("max_consecutive_failures", sa.Integer(), nullable=True),
        )
    if "run_timeout_seconds" not in sch_cols:
        op.add_column(
            "workflow_schedules",
            sa.Column("run_timeout_seconds", sa.Integer(), nullable=True),
        )
    if "api_call_budget" not in sch_cols:
        op.add_column(
            "workflow_schedules",
            sa.Column("api_call_budget", sa.Integer(), nullable=True),
        )
    if "execution_policy" not in sch_cols:
        op.add_column(
            "workflow_schedules",
            sa.Column("execution_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )


def downgrade() -> None:
    op.drop_column("workflow_schedules", "execution_policy")
    op.drop_column("workflow_schedules", "api_call_budget")
    op.drop_column("workflow_schedules", "run_timeout_seconds")
    op.drop_column("workflow_schedules", "max_consecutive_failures")
    op.drop_column("workflow_schedules", "consecutive_failures")
    op.drop_column("workflow_schedules", "max_concurrency")
    op.drop_column("workflow_schedules", "allow_overlap")
    op.drop_column("workflow_runs", "notification_sent")
    op.drop_index("idx_workflow_runs_schedule_id", table_name="workflow_runs")
    op.drop_column("workflow_runs", "schedule_id")

