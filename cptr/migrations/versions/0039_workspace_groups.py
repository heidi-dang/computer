"""Add WorkspaceGroup and WorkspaceGroupMember tables.

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_groups",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("group_type", sa.Text(), nullable=False, server_default="cross-repo"),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "slug", name="uq_workspace_group_user_slug"),
    )
    op.create_index("ix_workspace_groups_user_name", "workspace_groups", ["user_id", "name"])
    op.create_index("ix_workspace_groups_user_slug", "workspace_groups", ["user_id", "slug"])

    op.create_table(
        "workspace_group_members",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("role", sa.Text(), nullable=False, server_default="member"),
        sa.Column("primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("alias", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["workspace_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id", "workspace_id", name="uq_workspace_group_member_group_workspace"
        ),
    )
    op.create_index(
        "ix_workspace_group_members_group_sort",
        "workspace_group_members",
        ["group_id", "sort_order"],
    )
    op.create_index(
        "ix_workspace_group_members_workspace",
        "workspace_group_members",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_group_members_workspace",
        table_name="workspace_group_members",
    )
    op.drop_index(
        "ix_workspace_group_members_group_sort",
        table_name="workspace_group_members",
    )
    op.drop_table("workspace_group_members")
    op.drop_index("ix_workspace_groups_user_slug", table_name="workspace_groups")
    op.drop_index("ix_workspace_groups_user_name", table_name="workspace_groups")
    op.drop_table("workspace_groups")
