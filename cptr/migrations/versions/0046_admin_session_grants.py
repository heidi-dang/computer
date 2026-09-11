"""Add bounded Workbench ADMIN grants and audit events.

Revision ID: 0046
Revises: 0045
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_session_grants",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("workbench_session_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("granted_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workbench_session_id"], ["workbench_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_session_grant_user_session_granted",
        "admin_session_grants",
        ["user_id", "workbench_session_id", "granted_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_session_grant_session_active",
        "admin_session_grants",
        ["workbench_session_id", "revoked_at"],
        unique=False,
    )
    op.create_table(
        "admin_session_grant_events",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("workbench_session_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workbench_session_id"], ["workbench_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_session_grant_events_user_created",
        "admin_session_grant_events",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_session_grant_events_session_created",
        "admin_session_grant_events",
        ["workbench_session_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_admin_session_grant_events_session_created",
        table_name="admin_session_grant_events",
    )
    op.drop_index(
        "ix_admin_session_grant_events_user_created",
        table_name="admin_session_grant_events",
    )
    op.drop_table("admin_session_grant_events")
    op.drop_index("ix_admin_session_grant_session_active", table_name="admin_session_grants")
    op.drop_index("ix_admin_session_grant_user_session_granted", table_name="admin_session_grants")
    op.drop_table("admin_session_grants")
