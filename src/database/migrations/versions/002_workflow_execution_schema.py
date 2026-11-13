"""Workflow execution foundation schema for steps, runs, and dependencies.

Revision ID: 002_workflow_schema
Revises: 001_memory_schema
Create Date: 2026-11-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_workflow_schema"
down_revision: Union[str, None] = "001_memory_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Ensure Workflows Table exists (or add columns if existing)
    # Check if 'workflows' table exists via inspector
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "workflows" not in tables:
        op.create_table(
            "workflows",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_one_time", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
            sa.Column("current_step_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_steps", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("steps", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("context_data", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("execution_history", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("checkpoint_data", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("idx_workflows_user_id", "workflows", ["user_id"])
        op.create_index("idx_workflows_status", "workflows", ["status"])
        op.create_index("idx_workflows_user_status", "workflows", ["user_id", "status"])
    else:
        columns = [c["name"] for c in inspector.get_columns("workflows")]
        if "is_one_time" not in columns:
            op.add_column("workflows", sa.Column("is_one_time", sa.Boolean(), nullable=False, server_default=sa.text("true")))

    # 2. Workflow Steps Table
    if "workflow_steps" not in tables:
        op.create_table(
            "workflow_steps",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("workflow_id", sa.String(length=36), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
            sa.Column("step_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("tool_name", sa.String(length=100), nullable=False),
            sa.Column("input_template", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("dependencies", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("condition", sa.JSON(), nullable=True),
            sa.Column("max_retries", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
            sa.Column("output_data", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("idx_workflow_steps_workflow_id", "workflow_steps", ["workflow_id"])
        op.create_index("idx_workflow_steps_order", "workflow_steps", ["workflow_id", "step_order"])
        op.create_index("idx_workflow_steps_status", "workflow_steps", ["status"])

    # 3. Workflow Runs Table
    if "workflow_runs" not in tables:
        op.create_table(
            "workflow_runs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("workflow_id", sa.String(length=36), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
            sa.Column("trigger_type", sa.String(length=50), nullable=False, server_default="manual"),
            sa.Column("context_data", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("current_step_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("step_runs", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("idx_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"])
        op.create_index("idx_workflow_runs_user_id", "workflow_runs", ["user_id"])
        op.create_index("idx_workflow_runs_status", "workflow_runs", ["status"])
        op.create_index("idx_workflow_runs_user_status", "workflow_runs", ["user_id", "status"])


def downgrade() -> None:
    op.drop_table("workflow_runs")
    op.drop_table("workflow_steps")
    # workflows table can remain or be dropped depending on environment

