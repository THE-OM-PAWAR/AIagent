"""Workflow scheduler, recurrence rules, and distributed claims schema.

Revision ID: 004_workflow_scheduler
Revises: 003_workflow_reliability
Create Date: 2026-11-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004_workflow_scheduler"
down_revision: Union[str, None] = "003_workflow_reliability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "workflow_schedules" not in tables:
        op.create_table(
            "workflow_schedules",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("workflow_id", sa.String(length=36), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("trigger_type", sa.String(length=50), nullable=False),
            sa.Column("trigger_config", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="active"),
            sa.Column("catch_up_policy", sa.String(length=50), nullable=False, server_default="run_once"),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("total_runs_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_runs", sa.Integer(), nullable=True),
            sa.Column("claim_token", sa.String(length=64), nullable=True),
            sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("idx_workflow_schedules_workflow_id", "workflow_schedules", ["workflow_id"])
        op.create_index("idx_workflow_schedules_user_id", "workflow_schedules", ["user_id"])
        op.create_index("idx_workflow_schedules_status", "workflow_schedules", ["status"])
        op.create_index("idx_workflow_schedules_next_run", "workflow_schedules", ["next_run_at"])
        op.create_index("idx_workflow_schedules_due", "workflow_schedules", ["status", "next_run_at"])


def downgrade() -> None:
    op.drop_table("workflow_schedules")

