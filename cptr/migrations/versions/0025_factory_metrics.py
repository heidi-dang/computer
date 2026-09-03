"""Add durable Dark Factory metric and verified capability outcome projections.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "factory_metric_projections",
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
            nullable=True,
        ),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("dimension_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("attempts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("repair_iterations", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("regressions", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("runtime_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("gate_latency_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("verified_outcome", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_factory_metric_run_scope",
        "factory_metric_projections",
        ["run_id", "scope", "dimension_key"],
    )
    op.create_index(
        "ix_factory_metric_cycle_scope",
        "factory_metric_projections",
        ["cycle_id", "scope", "dimension_key"],
    )

    op.create_table(
        "factory_capability_outcomes",
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
        sa.Column(
            "capability_id",
            sa.Text(),
            sa.ForeignKey("factory_capabilities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repository_family", sa.Text(), nullable=False),
        sa.Column("task_family", sa.Text(), nullable=False),
        sa.Column("verified_success", sa.Boolean(), nullable=False),
        sa.Column(
            "proof_event_id",
            sa.Text(),
            sa.ForeignKey("factory_events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("regression", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("repair_iterations", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("runtime_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "cycle_id",
            "capability_id",
            name="uq_factory_capability_outcome_run_cycle_capability",
        ),
    )
    op.create_index(
        "ix_factory_capability_outcome_family",
        "factory_capability_outcomes",
        ["capability_id", "repository_family", "task_family"],
    )
    op.create_index(
        "ix_factory_capability_outcome_run",
        "factory_capability_outcomes",
        ["run_id", "cycle_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_factory_capability_outcome_run", table_name="factory_capability_outcomes")
    op.drop_index("ix_factory_capability_outcome_family", table_name="factory_capability_outcomes")
    op.drop_table("factory_capability_outcomes")
    op.drop_index("ix_factory_metric_cycle_scope", table_name="factory_metric_projections")
    op.drop_index("ix_factory_metric_run_scope", table_name="factory_metric_projections")
    op.drop_table("factory_metric_projections")
