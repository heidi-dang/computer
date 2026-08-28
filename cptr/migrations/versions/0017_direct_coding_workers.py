"""Add model-free direct coding workers.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "direct_coding_workers",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "workspace_id",
            sa.Text(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("responsibility", sa.Text(), nullable=False, server_default=""),
        sa.Column("repo_path", sa.Text(), nullable=False, server_default="."),
        sa.Column("status", sa.Text(), nullable=False, server_default="READY"),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("worktree_path", sa.Text(), nullable=False),
        sa.Column("base_revision", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("last_activity_at", sa.BigInteger(), nullable=True),
        sa.Column("integrated_at", sa.BigInteger(), nullable=True),
        sa.Column("closed_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("user_id", "branch", name="uq_direct_coding_worker_user_branch"),
    )
    op.create_index(
        "ix_direct_coding_worker_user_workspace_status",
        "direct_coding_workers",
        ["user_id", "workspace_id", "status"],
    )
    op.create_index(
        "ix_direct_coding_worker_user_updated",
        "direct_coding_workers",
        ["user_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_direct_coding_worker_user_updated", table_name="direct_coding_workers")
    op.drop_index(
        "ix_direct_coding_worker_user_workspace_status", table_name="direct_coding_workers"
    )
    op.drop_table("direct_coding_workers")
