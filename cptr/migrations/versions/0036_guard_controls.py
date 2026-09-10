"""Add owner-scoped Guard Control settings and audit events.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guard_settings",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("guard_id", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "guard_id"),
    )
    op.create_index(
        "ix_guard_settings_user_updated",
        "guard_settings",
        ["user_id", "updated_at"],
    )

    op.create_table(
        "guard_setting_events",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("guard_id", sa.Text(), nullable=False),
        sa.Column("previous_enabled", sa.Boolean(), nullable=False),
        sa.Column("new_enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_guard_setting_events_user_created",
        "guard_setting_events",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_guard_setting_events_guard_created",
        "guard_setting_events",
        ["guard_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_guard_setting_events_guard_created", table_name="guard_setting_events"
    )
    op.drop_index(
        "ix_guard_setting_events_user_created", table_name="guard_setting_events"
    )
    op.drop_table("guard_setting_events")
    op.drop_index("ix_guard_settings_user_updated", table_name="guard_settings")
    op.drop_table("guard_settings")
