"""add durable FlowDeck approval records

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flowdeck_approvals",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("capability", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.BigInteger(), nullable=False),
        sa.Column("resolved_at", sa.BigInteger(), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("evidence", sqlite.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id"),
    )
    op.create_index("ix_flowdeck_approvals_run_id", "flowdeck_approvals", ["run_id"])
    op.create_index("ix_flowdeck_approvals_status", "flowdeck_approvals", ["status"])


def downgrade() -> None:
    op.drop_index("ix_flowdeck_approvals_status", table_name="flowdeck_approvals")
    op.drop_index("ix_flowdeck_approvals_run_id", table_name="flowdeck_approvals")
    op.drop_table("flowdeck_approvals")