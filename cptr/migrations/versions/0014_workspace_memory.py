"""Add durable owner-scoped workspace memory.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_memory_streams",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "workspace_id", sa.Text(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_through_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("workspace_fingerprint", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("user_id", "workspace_id", name="uq_workspace_memory_stream_owner_workspace"),
    )
    op.create_index(
        "ix_workspace_memory_stream_owner_updated",
        "workspace_memory_streams",
        ["user_id", "updated_at"],
    )

    op.create_table(
        "workspace_memory_events",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "stream_id",
            sa.Text(),
            sa.ForeignKey("workspace_memory_streams.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "workspace_id", sa.Text(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default="mcp"),
        sa.Column(
            "session_id", sa.Text(), sa.ForeignKey("workbench_sessions.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("tool_name", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False, server_default="COMPLETE"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("affected_paths", sa.JSON(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("workspace_fingerprint", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("stream_id", "sequence", name="uq_workspace_memory_event_stream_sequence"),
        sa.UniqueConstraint(
            "user_id", "workspace_id", "operation_id", "kind", name="uq_workspace_memory_event_operation"
        ),
    )
    op.create_index(
        "ix_workspace_memory_event_owner_workspace_sequence",
        "workspace_memory_events",
        ["user_id", "workspace_id", "sequence"],
    )
    op.create_index("ix_workspace_memory_event_session", "workspace_memory_events", ["session_id", "sequence"])

    op.create_table(
        "workspace_memory_facts",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "workspace_id", sa.Text(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("category", sa.Text(), nullable=False, server_default="note"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("paths", sa.JSON(), nullable=False),
        sa.Column(
            "source_event_id",
            sa.Text(),
            sa.ForeignKey("workspace_memory_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("verified_fingerprint", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_workspace_memory_fact_owner_workspace_status",
        "workspace_memory_facts",
        ["user_id", "workspace_id", "status"],
    )
    op.create_index(
        "ix_workspace_memory_fact_owner_workspace_updated",
        "workspace_memory_facts",
        ["user_id", "workspace_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_memory_fact_owner_workspace_updated", table_name="workspace_memory_facts")
    op.drop_index("ix_workspace_memory_fact_owner_workspace_status", table_name="workspace_memory_facts")
    op.drop_table("workspace_memory_facts")
    op.drop_index("ix_workspace_memory_event_session", table_name="workspace_memory_events")
    op.drop_index("ix_workspace_memory_event_owner_workspace_sequence", table_name="workspace_memory_events")
    op.drop_table("workspace_memory_events")
    op.drop_index("ix_workspace_memory_stream_owner_updated", table_name="workspace_memory_streams")
    op.drop_table("workspace_memory_streams")
