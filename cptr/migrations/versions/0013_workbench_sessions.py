"""Add durable Workbench Sessions and replayable session events.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workbench_sessions",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="OPEN"),
        sa.Column("active_target_type", sa.Text(), nullable=True),
        sa.Column("active_target_id", sa.Text(), nullable=True),
        sa.Column("active_workspace_id", sa.Text(), nullable=True),
        sa.Column("event_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("last_event_at", sa.BigInteger(), nullable=True),
        sa.Column("archived_at", sa.BigInteger(), nullable=True),
        sa.Column("deleted_at", sa.BigInteger(), nullable=True),
        sa.Column("delete_requested_at", sa.BigInteger(), nullable=True),
        sa.Column("delete_confirmation_hash", sa.Text(), nullable=True),
        sa.Column("delete_confirmation_expires_at", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_workbench_session_user_status_updated",
        "workbench_sessions",
        ["user_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_workbench_session_user_last_event",
        "workbench_sessions",
        ["user_id", "last_event_at"],
    )
    op.create_table(
        "workbench_session_events",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("workbench_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("workspace_id", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("session_id", "sequence", name="uq_workbench_session_event_sequence"),
    )
    op.create_index(
        "ix_workbench_session_event_user_session_sequence",
        "workbench_session_events",
        ["user_id", "session_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_workbench_session_event_user_session_sequence", table_name="workbench_session_events")
    op.drop_table("workbench_session_events")
    op.drop_index("ix_workbench_session_user_last_event", table_name="workbench_sessions")
    op.drop_index("ix_workbench_session_user_status_updated", table_name="workbench_sessions")
    op.drop_table("workbench_sessions")
