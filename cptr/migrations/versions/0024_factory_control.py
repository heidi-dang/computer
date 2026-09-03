"""Add durable Dark Factory control approvals.

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "factory_approvals",
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
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("operation_digest", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("remote", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("decision_idempotency_key", sa.Text(), nullable=True),
        sa.Column("decision_digest", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("decided_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint(
            "cycle_id",
            "kind",
            "operation_digest",
            name="uq_factory_approval_cycle_operation",
        ),
        sa.UniqueConstraint(
            "run_id",
            "decision_idempotency_key",
            name="uq_factory_approval_run_decision_idempotency",
        ),
    )
    op.create_index(
        "ix_factory_approval_run_status",
        "factory_approvals",
        ["run_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_factory_approval_cycle_kind",
        "factory_approvals",
        ["cycle_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index("ix_factory_approval_cycle_kind", table_name="factory_approvals")
    op.drop_index("ix_factory_approval_run_status", table_name="factory_approvals")
    op.drop_table("factory_approvals")
