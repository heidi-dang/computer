"""Add durable control-plane and autonomous-supervisor records.

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
        "control_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("chat_id", sa.Text(), sa.ForeignKey("chats.id"), nullable=False),
        sa.Column("message_id", sa.Text(), sa.ForeignKey("chat_messages.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("cancelled_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_control_task_user_idempotency"),
    )
    op.create_index("ix_control_task_user_workspace", "control_tasks", ["user_id", "workspace_id"])

    op.create_table(
        "autonomous_monitors",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("goal_id", sa.Text(), nullable=False, unique=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("original_goal", sa.Text(), nullable=False),
        sa.Column("original_acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_scope_id", sa.Text(), nullable=True),
        sa.Column("approval_id", sa.Text(), nullable=True),
        sa.Column("lock_token", sa.Text(), nullable=True),
        sa.Column("lock_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("director_state", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_autonomous_monitor_user_status", "autonomous_monitors", ["user_id", "status"]
    )

    op.create_table(
        "autonomous_scopes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "monitor_id",
            sa.Text(),
            sa.ForeignKey("autonomous_monitors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("worker_task_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("verification_evidence", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("failure_evidence", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("last_decision", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("next_action", sa.Text(), nullable=True),
        sa.Column("history", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_autonomous_scope_monitor_ordinal", "autonomous_scopes", ["monitor_id", "ordinal"]
    )

    op.create_table(
        "autonomous_evidence",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "monitor_id",
            sa.Text(),
            sa.ForeignKey("autonomous_monitors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scope_id",
            sa.Text(),
            sa.ForeignKey("autonomous_scopes.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )

    op.create_table(
        "autonomous_approvals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "monitor_id",
            sa.Text(),
            sa.ForeignKey("autonomous_monitors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.BigInteger(), nullable=False),
        sa.Column("decided_at", sa.BigInteger(), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
    )

    op.create_table(
        "control_idempotency",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("user_id", "key", name="uq_control_idempotency_user_key"),
    )


def downgrade() -> None:
    op.drop_table("control_idempotency")
    op.drop_table("autonomous_approvals")
    op.drop_table("autonomous_evidence")
    op.drop_index("ix_autonomous_scope_monitor_ordinal", table_name="autonomous_scopes")
    op.drop_table("autonomous_scopes")
    op.drop_index("ix_autonomous_monitor_user_status", table_name="autonomous_monitors")
    op.drop_table("autonomous_monitors")
    op.drop_index("ix_control_task_user_workspace", table_name="control_tasks")
    op.drop_table("control_tasks")
