"""Add durable task review checkpoint state and evidence.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "control_tasks",
        sa.Column("review_status", sa.Text(), nullable=False, server_default="NOT_REQUIRED"),
    )
    op.add_column("control_tasks", sa.Column("review_summary", sa.JSON(), nullable=True))
    op.add_column("control_tasks", sa.Column("review_decision", sa.JSON(), nullable=True))
    op.add_column("control_tasks", sa.Column("review_ready_at", sa.BigInteger(), nullable=True))
    op.add_column("control_tasks", sa.Column("reviewed_at", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("control_tasks", "reviewed_at")
    op.drop_column("control_tasks", "review_ready_at")
    op.drop_column("control_tasks", "review_decision")
    op.drop_column("control_tasks", "review_summary")
    op.drop_column("control_tasks", "review_status")
