"""Workflow reliability, execution lease concurrency, and human-in-the-loop approvals schema.

Revision ID: 003_workflow_reliability
Revises: 002_workflow_schema
Create Date: 2026-11-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_workflow_reliability"
down_revision: Union[str, None] = "002_workflow_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    # 1. Add Lease & Idempotency columns to workflow_runs if not present
    if "workflow_runs" in tables:
        columns = [c["name"] for c in inspector.get_columns("workflow_runs")]
        if "lease_token" not in columns:
            op.add_column("workflow_runs", sa.Column("lease_token", sa.String(length=64), nullable=True))
        if "lease_expires_at" not in columns:
            op.add_column("workflow_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        if "idempotency_key" not in columns:
            op.add_column("workflow_runs", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
            op.create_index("idx_workflow_runs_idempotency_key", "workflow_runs", ["idempotency_key"])

    # 2. Create workflow_approvals table if not exists
    if "workflow_approvals" not in tables:
        op.create_table(
            "workflow_approvals",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("workflow_id", sa.String(length=36), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
            sa.Column("run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("step_name", sa.String(length=100), nullable=False),
            sa.Column("tool_name", sa.String(length=100), nullable=False),
            sa.Column("action_summary", sa.Text(), nullable=False),
            sa.Column("action_arguments", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("requested_by_user_id", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
            sa.Column("resolved_by_user_id", sa.BigInteger(), nullable=True),
            sa.Column("resolution_notes", sa.Text(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("idx_workflow_approvals_workflow_id", "workflow_approvals", ["workflow_id"])
        op.create_index("idx_workflow_approvals_run_id", "workflow_approvals", ["run_id"])
        op.create_index("idx_workflow_approvals_user_status", "workflow_approvals", ["requested_by_user_id", "status"])
        op.create_index("idx_workflow_approvals_expires", "workflow_approvals", ["expires_at"])


def downgrade() -> None:
    op.drop_table("workflow_approvals")
    op.drop_column("workflow_runs", "idempotency_key")
    op.drop_column("workflow_runs", "lease_expires_at")
    op.drop_column("workflow_runs", "lease_token")

