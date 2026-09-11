"""Add environment profiles and immutable environment profile versions.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "environment_profiles",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active_version_id", sa.Text(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_environment_profiles_user_name"),
    )
    op.create_index(
        "ix_environment_profiles_user",
        "environment_profiles",
        ["user_id"],
    )
    op.create_index(
        "ix_environment_profiles_workspace",
        "environment_profiles",
        ["workspace_id"],
    )
    op.create_index(
        "ix_environment_profiles_user_archived",
        "environment_profiles",
        ["user_id", "is_archived"],
    )

    op.create_table(
        "environment_profile_versions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("runtime_profile", sa.Text(), nullable=False, server_default="default"),
        sa.Column("environment_variables", sa.JSON(), nullable=False),
        sa.Column("packages", sa.JSON(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("credential_refs", sa.JSON(), nullable=False),
        sa.Column("parent_version_id", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["environment_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_version_id"], ["environment_profile_versions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_id", "version_number", name="uq_env_profile_version_number"),
        sa.UniqueConstraint("profile_id", "digest", name="uq_env_profile_version_digest"),
    )
    op.create_index(
        "ix_env_profile_versions_profile",
        "environment_profile_versions",
        ["profile_id"],
    )
    op.create_index(
        "ix_env_profile_versions_digest",
        "environment_profile_versions",
        ["digest"],
    )


def downgrade() -> None:
    op.drop_index("ix_env_profile_versions_digest", table_name="environment_profile_versions")
    op.drop_index("ix_env_profile_versions_profile", table_name="environment_profile_versions")
    op.drop_table("environment_profile_versions")
    op.drop_index("ix_environment_profiles_user_archived", table_name="environment_profiles")
    op.drop_index("ix_environment_profiles_workspace", table_name="environment_profiles")
    op.drop_index("ix_environment_profiles_user", table_name="environment_profiles")
    op.drop_table("environment_profiles")
