"""Add WorkspaceTask aggregation, repository pins, worker links, and verification evidence.

Revision ID: 0044
Revises: 0043
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_tasks",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.Text(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="OPEN"),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("closed_at", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_workspace_tasks_user_workspace_status",
        "workspace_tasks",
        ["user_id", "workspace_id", "status"],
    )
    op.create_index(
        "ix_workspace_tasks_user_updated",
        "workspace_tasks",
        ["user_id", "updated_at"],
    )

    op.create_table(
        "workspace_task_repository_pins",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("workspace_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repo_path", sa.Text(), nullable=False, server_default="."),
        sa.Column("pinned_revision", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("pinned_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("task_id", "repo_path", name="uq_workspace_task_repo_pin"),
    )
    op.create_index(
        "ix_workspace_task_pins_task_id",
        "workspace_task_repository_pins",
        ["task_id"],
    )

    op.create_table(
        "workspace_task_worker_links",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("workspace_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "worker_id",
            sa.Text(),
            sa.ForeignKey("direct_coding_workers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False, server_default="contributor"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("linked_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("task_id", "worker_id", name="uq_workspace_task_worker_link"),
    )
    op.create_index(
        "ix_workspace_task_worker_links_task_id",
        "workspace_task_worker_links",
        ["task_id"],
    )
    op.create_index(
        "ix_workspace_task_worker_links_worker_id",
        "workspace_task_worker_links",
        ["worker_id"],
    )

    op.create_table(
        "workspace_task_evidence",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("workspace_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("repo_path", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_workspace_task_evidence_task_kind",
        "workspace_task_evidence",
        ["task_id", "kind"],
    )
    op.create_index(
        "ix_workspace_task_evidence_task_status",
        "workspace_task_evidence",
        ["task_id", "status"],
    )
    op.create_index(
        "ix_workspace_task_evidence_task_created",
        "workspace_task_evidence",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_task_evidence_task_created",
        table_name="workspace_task_evidence",
    )
    op.drop_index(
        "ix_workspace_task_evidence_task_status",
        table_name="workspace_task_evidence",
    )
    op.drop_index(
        "ix_workspace_task_evidence_task_kind",
        table_name="workspace_task_evidence",
    )
    op.drop_table("workspace_task_evidence")

    op.drop_index(
        "ix_workspace_task_worker_links_worker_id",
        table_name="workspace_task_worker_links",
    )
    op.drop_index(
        "ix_workspace_task_worker_links_task_id",
        table_name="workspace_task_worker_links",
    )
    op.drop_table("workspace_task_worker_links")

    op.drop_index(
        "ix_workspace_task_pins_task_id",
        table_name="workspace_task_repository_pins",
    )
    op.drop_table("workspace_task_repository_pins")

    op.drop_index(
        "ix_workspace_tasks_user_updated",
        table_name="workspace_tasks",
    )
    op.drop_index(
        "ix_workspace_tasks_user_workspace_status",
        table_name="workspace_tasks",
    )
    op.drop_table("workspace_tasks")
