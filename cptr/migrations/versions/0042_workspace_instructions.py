"""Add Workspace instructions versioning and current pointer.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("current_instruction_version_id", sa.Text(), nullable=True),
    )
    op.create_table(
        "workspace_instruction_versions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "version", name="uq_workspace_instruction_version"),
    )
    op.create_index(
        "ix_workspace_instruction_versions_workspace_current",
        "workspace_instruction_versions",
        ["workspace_id", "is_current"],
    )
    op.create_index(
        "ix_workspace_instruction_versions_workspace_version",
        "workspace_instruction_versions",
        ["workspace_id", "version"],
    )
    op.create_index(
        "ix_workspace_instruction_versions_user_id",
        "workspace_instruction_versions",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_instruction_versions_user_id",
        table_name="workspace_instruction_versions",
    )
    op.drop_index(
        "ix_workspace_instruction_versions_workspace_version",
        table_name="workspace_instruction_versions",
    )
    op.drop_index(
        "ix_workspace_instruction_versions_workspace_current",
        table_name="workspace_instruction_versions",
    )
    op.drop_table("workspace_instruction_versions")
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("current_instruction_version_id")
