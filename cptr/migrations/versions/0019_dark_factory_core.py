"""Add the durable Dark Factory core domain.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "factory_runs",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "workspace_id",
            sa.Text(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mission", sa.Text(), nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("current_cycle_id", sa.Text(), nullable=True),
        sa.Column("resumable_state", sa.Text(), nullable=True),
        sa.Column("policy", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("budget", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("config_fingerprint", sa.Text(), nullable=False),
        sa.Column("next_action", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("lease_token", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("completed_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_factory_run_user_idempotency"),
    )
    op.create_index(
        "ix_factory_run_user_state_updated",
        "factory_runs",
        ["user_id", "state", "updated_at"],
    )
    op.create_index(
        "ix_factory_run_workspace_state", "factory_runs", ["workspace_id", "state"]
    )

    op.create_table(
        "factory_cycles",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("factory_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("selected_finding", sa.JSON(), nullable=True),
        sa.Column("capability_requirements", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("selected_capabilities", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("base_revision", sa.Text(), nullable=True),
        sa.Column("base_fingerprint", sa.Text(), nullable=True),
        sa.Column("target_revision", sa.Text(), nullable=True),
        sa.Column("target_fingerprint", sa.Text(), nullable=True),
        sa.Column("mutation_worker_id", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failure_signatures", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("next_action", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("completed_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_factory_cycle_run_ordinal"),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_factory_cycle_run_idempotency"
        ),
    )
    op.create_index("ix_factory_cycle_run_state", "factory_cycles", ["run_id", "state"])

    op.create_table(
        "factory_events",
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
            sa.ForeignKey("factory_cycles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("payload_digest", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_factory_event_run_sequence"),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_factory_event_run_idempotency"
        ),
    )
    op.create_index("ix_factory_event_run_created", "factory_events", ["run_id", "created_at"])

    op.create_table(
        "factory_evidence",
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
            sa.ForeignKey("factory_cycles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("gate_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("authority", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_factory_evidence_run_created", "factory_evidence", ["run_id", "created_at"]
    )
    op.create_index(
        "ix_factory_evidence_cycle_gate", "factory_evidence", ["cycle_id", "gate_id"]
    )

    op.create_table(
        "factory_gate_results",
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
        sa.Column("gate_id", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("applicable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("evaluated_revision", sa.Text(), nullable=True),
        sa.Column("evaluated_fingerprint", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("attempt", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "cycle_id",
            "gate_id",
            "attempt",
            name="uq_factory_gate_cycle_gate_attempt",
        ),
    )
    op.create_index(
        "ix_factory_gate_run_cycle", "factory_gate_results", ["run_id", "cycle_id"]
    )
    op.create_index(
        "ix_factory_gate_cycle_gate", "factory_gate_results", ["cycle_id", "gate_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_factory_gate_cycle_gate", table_name="factory_gate_results")
    op.drop_index("ix_factory_gate_run_cycle", table_name="factory_gate_results")
    op.drop_table("factory_gate_results")
    op.drop_index("ix_factory_evidence_cycle_gate", table_name="factory_evidence")
    op.drop_index("ix_factory_evidence_run_created", table_name="factory_evidence")
    op.drop_table("factory_evidence")
    op.drop_index("ix_factory_event_run_created", table_name="factory_events")
    op.drop_table("factory_events")
    op.drop_index("ix_factory_cycle_run_state", table_name="factory_cycles")
    op.drop_table("factory_cycles")
    op.drop_index("ix_factory_run_workspace_state", table_name="factory_runs")
    op.drop_index("ix_factory_run_user_state_updated", table_name="factory_runs")
    op.drop_table("factory_runs")
