"""Add durable Workbench-scoped local root grants.

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "local_root_grants",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("workbench_session_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("granted_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workbench_session_id"], ["workbench_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_local_root_grant_user_session_granted",
        "local_root_grants",
        ["user_id", "workbench_session_id", "granted_at"],
        unique=False,
    )
    op.create_index(
        "ix_local_root_grant_session_active",
        "local_root_grants",
        ["workbench_session_id", "revoked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_local_root_grant_session_active", table_name="local_root_grants")
    op.drop_index("ix_local_root_grant_user_session_granted", table_name="local_root_grants")
    op.drop_table("local_root_grants")
