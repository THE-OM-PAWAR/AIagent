"""Automation templates configuration, findings persistence, and deduplication schema.

Revision ID: 006_automation_templates_and_findings
Revises: 005_recurring_workflow_lifecycle
Create Date: 2026-11-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006_automation_templates_and_findings"
down_revision: Union[str, None] = "005_recurring_workflow_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # 1. automation_configs table
    if "automation_configs" not in existing_tables:
        op.create_table(
            "automation_configs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("automation_type", sa.String(length=50), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column(
                "workflow_id",
                sa.String(length=36),
                sa.ForeignKey("workflows.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "schedule_id",
                sa.String(length=36),
                sa.ForeignKey("workflow_schedules.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index("idx_automation_configs_user_id", "automation_configs", ["user_id"])
        op.create_index("idx_automation_configs_type", "automation_configs", ["automation_type"])
        op.create_index("idx_automation_configs_workflow_id", "automation_configs", ["workflow_id"])
        op.create_index("idx_automation_configs_schedule_id", "automation_configs", ["schedule_id"])

    # 2. automation_findings table
    if "automation_findings" not in existing_tables:
        op.create_table(
            "automation_findings",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("automation_type", sa.String(length=50), nullable=False),
            sa.Column(
                "automation_config_id",
                sa.String(length=36),
                sa.ForeignKey("automation_configs.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("dedup_key", sa.String(length=255), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("source_url", sa.String(length=1024), nullable=True),
            sa.Column("qualification_score", sa.Float(), nullable=False, server_default=sa.text("0.0")),
            sa.Column("status", sa.String(length=50), nullable=False, server_default=sa.text("'qualified'")),
            sa.Column("data", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("generated_draft", sa.JSON(), nullable=True),
            sa.Column("follow_up_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("workflow_id", sa.String(length=36), nullable=True),
            sa.Column("run_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("user_id", "automation_type", "dedup_key", name="uq_user_automation_finding_dedup"),
        )
        op.create_index("idx_automation_findings_user_id", "automation_findings", ["user_id"])
        op.create_index("idx_automation_findings_type", "automation_findings", ["automation_type"])
        op.create_index("idx_automation_findings_config_id", "automation_findings", ["automation_config_id"])
        op.create_index("idx_automation_findings_dedup_key", "automation_findings", ["dedup_key"])
        op.create_index("idx_automation_findings_status", "automation_findings", ["status"])
        op.create_index("idx_automation_findings_workflow_id", "automation_findings", ["workflow_id"])
        op.create_index("idx_automation_findings_run_id", "automation_findings", ["run_id"])


def downgrade() -> None:
    op.drop_table("automation_findings")
    op.drop_table("automation_configs")

