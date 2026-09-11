"""Add Workspace OS v2 sticky binding fields to Workbench Sessions.

Revision ID: 0043
Revises: 0042
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workbench_sessions") as batch:
        batch.add_column(sa.Column("environment_profile_id", sa.Text(), nullable=True))
        batch.add_column(sa.Column("environment_profile_override", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("admin_role", sa.Text(), nullable=True))
        batch.add_column(sa.Column("role_context", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("last_context_snapshot_id", sa.Text(), nullable=True))
        batch.create_index(
            "ix_workbench_session_user_workspace",
            ["user_id", "workspace_id"],
        )
        batch.create_index(
            "ix_workbench_session_snapshot",
            ["last_context_snapshot_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("workbench_sessions") as batch:
        batch.drop_index("ix_workbench_session_snapshot")
        batch.drop_index("ix_workbench_session_user_workspace")
        batch.drop_column("last_context_snapshot_id")
        batch.drop_column("role_context")
        batch.drop_column("admin_role")
        batch.drop_column("environment_profile_override")
        batch.drop_column("environment_profile_id")
