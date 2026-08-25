"""Persist bounded CPTR Live Workbench event envelopes."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "control_live_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("target_key", sa.Text(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("monitor_id", sa.Text(), nullable=True),
        sa.Column("worker_task_id", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("target_key", "sequence", name="uq_control_live_event_target_sequence"),
    )
    op.create_index(
        "ix_control_live_event_target_created",
        "control_live_events",
        ["target_key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_control_live_event_target_created", table_name="control_live_events")
    op.drop_table("control_live_events")
