"""Initial memory foundation schema and indexes.

Revision ID: 001_memory_schema
Revises: None
Create Date: 2026-11-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_memory_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Memories Table
    op.create_table(
        "memories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_reference", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("project_association", sa.String(length=128), nullable=True),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("fact_type", sa.String(length=32), nullable=False, server_default="explicit_fact"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_extra", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_memories_user_id", "memories", ["user_id"])
    op.create_index("idx_memories_category", "memories", ["category"])
    op.create_index("idx_memories_status", "memories", ["status"])
    op.create_index("idx_memories_user_status", "memories", ["user_id", "status"])
    op.create_index("idx_memories_user_category", "memories", ["user_id", "category"])
    op.create_index("idx_memories_user_project", "memories", ["user_id", "project_association"])
    op.create_index("idx_memories_created_at", "memories", ["created_at"])
    op.create_index("idx_memories_expires_at", "memories", ["expires_at"])

    # Full text GIN index for PostgreSQL
    bind = op.get_bind()
    if bind and bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX idx_memories_content_fts ON memories USING gin(to_tsvector('english', content));"
        )

    # 2. Memory Relationships Table
    op.create_table(
        "memory_relationships",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_memory_id", sa.String(length=36), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_memory_id", sa.String(length=36), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relationship_type", sa.String(length=64), nullable=False),
        sa.Column("metadata_extra", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_mem_rel_user_source", "memory_relationships", ["user_id", "source_memory_id"])
    op.create_index("idx_mem_rel_user_target", "memory_relationships", ["user_id", "target_memory_id"])
    op.create_index("idx_mem_rel_user_type", "memory_relationships", ["user_id", "relationship_type"])

    # 3. Memory Conflicts Table
    op.create_table(
        "memory_conflicts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("existing_memory_id", sa.String(length=36), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conflicting_content", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="unresolved"),
        sa.Column("source_reference", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_conflicts_user_status", "memory_conflicts", ["user_id", "status"])

    # 4. Memory Outbox Table
    op.create_table(
        "memory_outbox",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("memory_id", sa.String(length=36), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_outbox_status_retry", "memory_outbox", ["status", "retry_count"])
    op.create_index("idx_outbox_created", "memory_outbox", ["created_at"])

    # 5. Conversations & Messages Tables
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=64), unique=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_extra", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_conversations_user_active", "conversations", ["user_id", "is_active", "updated_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=64), sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_messages_conv_created", "messages", ["conversation_id", "created_at"])
    op.create_index("idx_messages_user_created", "messages", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("memory_outbox")
    op.drop_table("memory_conflicts")
    op.drop_table("memory_relationships")
    op.drop_table("memories")

