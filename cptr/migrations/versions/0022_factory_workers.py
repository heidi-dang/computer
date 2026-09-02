"""Add durable Dark Factory worker ownership.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "factory_worker_assignments",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("factory_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "cycle_id",
            sa.Text(),
            sa.ForeignKey("factory_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("owner_key", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("repo_path", sa.Text(), nullable=False, server_default="."),
        sa.Column("scope", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("base_revision", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("closed_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("worker_id", name="uq_factory_worker_assignment_worker"),
    )
    op.create_index(
        "ix_factory_worker_assignment_run_cycle_status",
        "factory_worker_assignments",
        ["run_id", "cycle_id", "status"],
    )
    op.create_index(
        "ix_factory_worker_assignment_workspace_mode_status",
        "factory_worker_assignments",
        ["workspace_id", "mode", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_factory_worker_assignment_workspace_mode_status",
        table_name="factory_worker_assignments",
    )
    op.drop_index(
        "ix_factory_worker_assignment_run_cycle_status",
        table_name="factory_worker_assignments",
    )
    op.drop_table("factory_worker_assignments")
