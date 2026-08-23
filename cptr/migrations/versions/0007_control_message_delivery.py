"""Persist queued control messages and delivery acknowledgements.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "control_messages",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("control_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chat_id", sa.Text(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("chat_message_id", sa.Text(), sa.ForeignKey("chat_messages.id"), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="QUEUED"),
        sa.Column("target_message_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("delivered_at", sa.BigInteger(), nullable=True),
        sa.Column("consumed_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("user_id", "task_id", "dedupe_key", name="uq_control_message_dedupe"),
    )
    op.create_index(
        "ix_control_message_chat_status", "control_messages", ["chat_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_control_message_chat_status", table_name="control_messages")
    op.drop_table("control_messages")
