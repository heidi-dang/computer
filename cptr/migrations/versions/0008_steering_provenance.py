"""Persist autonomous steering provenance and exactly-once consumption."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "autonomous_scopes",
        sa.Column("steering_requests", sa.JSON(), nullable=False, server_default="[]"),
    )
    for name in (
        "monitor_id",
        "scope_id",
        "intended_message_id",
        "consumed_task_id",
        "consumed_message_id",
    ):
        op.add_column("control_messages", sa.Column(name, sa.Text(), nullable=True))
    op.create_index(
        "ix_control_message_monitor_scope",
        "control_messages",
        ["monitor_id", "scope_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_control_message_monitor_scope", table_name="control_messages")
    for name in (
        "consumed_message_id",
        "consumed_task_id",
        "intended_message_id",
        "scope_id",
        "monitor_id",
    ):
        op.drop_column("control_messages", name)
    op.drop_column("autonomous_scopes", "steering_requests")
