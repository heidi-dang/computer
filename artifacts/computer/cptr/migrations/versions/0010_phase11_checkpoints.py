"""add durable Phase 11 checkpoint identities

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flowdeck_checkpoints",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("evidence", sqlite.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("restored_at", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_flowdeck_checkpoints_run_id", "flowdeck_checkpoints", ["run_id"]
    )
    op.create_index(
        "ix_flowdeck_checkpoints_workspace_created",
        "flowdeck_checkpoints",
        ["workspace", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_flowdeck_checkpoints_workspace_created",
        table_name="flowdeck_checkpoints",
    )
    op.drop_index("ix_flowdeck_checkpoints_run_id", table_name="flowdeck_checkpoints")
    op.drop_table("flowdeck_checkpoints")