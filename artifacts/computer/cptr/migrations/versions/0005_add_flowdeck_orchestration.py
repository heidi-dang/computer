"""add durable FlowDeck orchestration state

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flowdeck_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("request_key", sa.Text(), nullable=False, unique=True),
        sa.Column("workspace", sa.Text(), nullable=True),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("heartbeat_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_flowdeck_runs_status_heartbeat", "flowdeck_runs", ["status", "heartbeat_at"])
    op.create_table(
        "flowdeck_steps",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_flowdeck_step_sequence"),
    )
    op.create_index("ix_flowdeck_steps_run_id", "flowdeck_steps", ["run_id"])
    op.create_table(
        "flowdeck_logical_operations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("capability", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("reconcile_kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("intent_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("authoritative_evidence", sqlite.JSON(), nullable=True),
    )
    op.create_index("ix_flowdeck_logical_operations_run_id", "flowdeck_logical_operations", ["run_id"])
    op.create_index("ix_flowdeck_logical_operations_step_id", "flowdeck_logical_operations", ["step_id"])
    op.create_table(
        "flowdeck_physical_attempts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("heartbeat_at", sa.BigInteger(), nullable=True),
        sa.Column("ended_at", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("operation_id", "attempt_no", name="uq_flowdeck_attempt_number"),
    )
    op.create_index("ix_flowdeck_physical_attempts_operation_id", "flowdeck_physical_attempts", ["operation_id"])
    op.create_table(
        "flowdeck_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", sqlite.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_flowdeck_event_sequence"),
    )
    op.create_index("ix_flowdeck_events_run_id", "flowdeck_events", ["run_id"])
    op.create_table(
        "flowdeck_workspace_leases",
        sa.Column("workspace", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.BigInteger(), nullable=False),
        sa.Column("heartbeat_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "flowdeck_recovery_leases",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.BigInteger(), nullable=False),
        sa.Column("heartbeat_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("flowdeck_recovery_leases")
    op.drop_table("flowdeck_workspace_leases")
    op.drop_index("ix_flowdeck_events_run_id", table_name="flowdeck_events")
    op.drop_table("flowdeck_events")
    op.drop_index("ix_flowdeck_physical_attempts_operation_id", table_name="flowdeck_physical_attempts")
    op.drop_table("flowdeck_physical_attempts")
    op.drop_index("ix_flowdeck_logical_operations_step_id", table_name="flowdeck_logical_operations")
    op.drop_index("ix_flowdeck_logical_operations_run_id", table_name="flowdeck_logical_operations")
    op.drop_table("flowdeck_logical_operations")
    op.drop_index("ix_flowdeck_steps_run_id", table_name="flowdeck_steps")
    op.drop_table("flowdeck_steps")
    op.drop_index("ix_flowdeck_runs_status_heartbeat", table_name="flowdeck_runs")
    op.drop_table("flowdeck_runs")