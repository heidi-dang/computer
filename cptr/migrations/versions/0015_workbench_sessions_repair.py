"""Repair Workbench Session schema after the historical experimental 0014 collision.

Revision ID: 0015
Revises: 0014

An unpublished/experimental workspace-memory branch previously used revision
0014. Machines that ran that branch can therefore be stamped at 0014 without
the Workbench Session tables introduced by the current 0014 migration. This
migration is intentionally idempotent: clean databases already have the tables
and no-op, while affected databases receive the missing schema.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table_name: str) -> set[str]:
    return {
        str(index.get("name"))
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def upgrade() -> None:
    tables = _table_names()
    if "workbench_sessions" not in tables:
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
        tables.add("workbench_sessions")

    session_indexes = _index_names("workbench_sessions")
    if "ix_workbench_session_user_status_updated" not in session_indexes:
        op.create_index(
            "ix_workbench_session_user_status_updated",
            "workbench_sessions",
            ["user_id", "status", "updated_at"],
        )
    if "ix_workbench_session_user_last_event" not in session_indexes:
        op.create_index(
            "ix_workbench_session_user_last_event",
            "workbench_sessions",
            ["user_id", "last_event_at"],
        )

    if "workbench_session_events" not in tables:
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
            sa.UniqueConstraint(
                "session_id", "sequence", name="uq_workbench_session_event_sequence"
            ),
        )

    event_indexes = _index_names("workbench_session_events")
    if "ix_workbench_session_event_user_session_sequence" not in event_indexes:
        op.create_index(
            "ix_workbench_session_event_user_session_sequence",
            "workbench_session_events",
            ["user_id", "session_id", "sequence"],
        )


def downgrade() -> None:
    # Revision 0014 owns the canonical Workbench Session schema. Keeping this
    # repair downgrade as a no-op lets a subsequent downgrade through 0014
    # remove the tables exactly once.
    pass
