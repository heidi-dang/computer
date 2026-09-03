"""Add Dark Factory orchestration, Git intent, and CI lifecycle state.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("factory_cycles") as batch:
        batch.add_column(sa.Column("gate_plan", sa.JSON(), nullable=False, server_default="{}"))
    with op.batch_alter_table("factory_evidence") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.Text(), nullable=True))
        batch.create_unique_constraint(
            "uq_factory_evidence_run_idempotency",
            ["run_id", "idempotency_key"],
        )
    with op.batch_alter_table("factory_gate_results") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.Text(), nullable=True))
        batch.create_unique_constraint(
            "uq_factory_gate_run_idempotency",
            ["run_id", "idempotency_key"],
        )

    op.create_table(
        "factory_commit_intents",
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
        sa.Column("repository_key", sa.Text(), nullable=False),
        sa.Column("verified_revision", sa.Text(), nullable=False),
        sa.Column("verified_fingerprint", sa.Text(), nullable=False),
        sa.Column("diff_digest", sa.Text(), nullable=False),
        sa.Column("changed_paths", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("commit_message", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PREPARED"),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        sa.Column("push_status", sa.Text(), nullable=True),
        sa.Column("push_remote", sa.Text(), nullable=True),
        sa.Column("push_branch", sa.Text(), nullable=True),
        sa.Column("push_approval_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("committed_at", sa.BigInteger(), nullable=True),
        sa.Column("pushed_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("cycle_id", name="uq_factory_commit_intent_cycle"),
    )
    op.create_index(
        "ix_factory_commit_intent_run_status",
        "factory_commit_intents",
        ["run_id", "status"],
    )
    op.create_index(
        "ix_factory_commit_intent_commit_sha",
        "factory_commit_intents",
        ["commit_sha"],
    )

    op.create_table(
        "factory_ci_runs",
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
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("repository", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("external_run_id", sa.Text(), nullable=False),
        sa.Column("check_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="QUEUED"),
        sa.Column("conclusion", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("diagnosis_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("diagnosis_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("last_observed_at", sa.BigInteger(), nullable=True),
        sa.Column("diagnosed_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint(
            "provider",
            "repository",
            "external_run_id",
            "check_id",
            name="uq_factory_ci_provider_run_check",
        ),
    )
    op.create_index(
        "ix_factory_ci_cycle_revision",
        "factory_ci_runs",
        ["cycle_id", "revision"],
    )
    op.create_index(
        "ix_factory_ci_run_status",
        "factory_ci_runs",
        ["run_id", "status", "conclusion"],
    )


def downgrade() -> None:
    op.drop_index("ix_factory_ci_run_status", table_name="factory_ci_runs")
    op.drop_index("ix_factory_ci_cycle_revision", table_name="factory_ci_runs")
    op.drop_table("factory_ci_runs")
    op.drop_index("ix_factory_commit_intent_commit_sha", table_name="factory_commit_intents")
    op.drop_index("ix_factory_commit_intent_run_status", table_name="factory_commit_intents")
    op.drop_table("factory_commit_intents")
    with op.batch_alter_table("factory_gate_results") as batch:
        batch.drop_constraint("uq_factory_gate_run_idempotency", type_="unique")
        batch.drop_column("idempotency_key")
    with op.batch_alter_table("factory_evidence") as batch:
        batch.drop_constraint("uq_factory_evidence_run_idempotency", type_="unique")
        batch.drop_column("idempotency_key")
    with op.batch_alter_table("factory_cycles") as batch:
        batch.drop_column("gate_plan")
