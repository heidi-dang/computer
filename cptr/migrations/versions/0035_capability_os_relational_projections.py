"""Add first-class relational projections for Capability OS lifecycle state.

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capability_os_runs",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("capability_digest", sa.Text(), nullable=False),
        sa.Column("lease_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("inputs_digest", sa.Text(), nullable=True),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["lease_id"], ["capability_os_leases.lease_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_capability_os_run_task_started", "capability_os_runs", ["task_id", "started_at_ms"]
    )
    op.create_index(
        "ix_capability_os_run_artifact_status",
        "capability_os_runs",
        ["capability_digest", "status"],
    )

    op.create_table(
        "capability_os_run_steps",
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("output_digest", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["capability_os_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("step_id"),
        sa.UniqueConstraint(
            "run_id", "node_id", "attempt", name="uq_capability_os_run_step_attempt"
        ),
    )
    op.create_index(
        "ix_capability_os_run_step_run_status", "capability_os_run_steps", ["run_id", "status"]
    )

    op.create_table(
        "capability_os_experiments",
        sa.Column("experiment_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("change_class", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("control_artifact_digest", sa.Text(), nullable=False),
        sa.Column("candidate_artifact_digest", sa.Text(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("min_runs_per_arm", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("experiment_id"),
    )
    op.create_index(
        "ix_capability_os_experiment_task_state", "capability_os_experiments", ["task_id", "state"]
    )
    op.create_index(
        "ix_capability_os_experiment_candidate",
        "capability_os_experiments",
        ["candidate_artifact_digest", "created_at_ms"],
    )

    op.create_table(
        "capability_os_experiment_runs",
        sa.Column("experiment_run_id", sa.Text(), nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=False),
        sa.Column("arm", sa.Text(), nullable=False),
        sa.Column("source_evidence_id", sa.Text(), nullable=False),
        sa.Column("source_evidence_digest", sa.Text(), nullable=False),
        sa.Column("success", sa.BigInteger(), nullable=False),
        sa.Column("regression", sa.BigInteger(), nullable=False),
        sa.Column("safety_events", sa.BigInteger(), nullable=False),
        sa.Column("cost", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["capability_os_experiments.experiment_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("experiment_run_id"),
        sa.UniqueConstraint(
            "experiment_id", "source_evidence_id", name="uq_capability_os_experiment_source"
        ),
    )
    op.create_index(
        "ix_capability_os_experiment_run_arm",
        "capability_os_experiment_runs",
        ["experiment_id", "arm", "created_at_ms"],
    )

    op.create_table(
        "capability_os_promotions",
        sa.Column("promotion_id", sa.Text(), nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=False),
        sa.Column("candidate_artifact_digest", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=False),
        sa.Column("target_state", sa.Text(), nullable=False),
        sa.Column("evaluation_evidence_id", sa.Text(), nullable=False),
        sa.Column("intent_evidence_id", sa.Text(), nullable=False),
        sa.Column("promotion_evidence_id", sa.Text(), nullable=False),
        sa.Column("owner_approval_verified", sa.BigInteger(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["capability_os_experiments.experiment_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("promotion_id"),
        sa.UniqueConstraint("promotion_evidence_id", name="uq_capability_os_promotion_evidence"),
    )
    op.create_index(
        "ix_capability_os_promotion_experiment",
        "capability_os_promotions",
        ["experiment_id", "created_at_ms"],
    )

    op.create_table(
        "capability_os_lineage_edges",
        sa.Column("edge_id", sa.Text(), nullable=False),
        sa.Column("child_digest", sa.Text(), nullable=False),
        sa.Column("parent_ref", sa.Text(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("edge_id"),
        sa.UniqueConstraint(
            "child_digest", "parent_ref", "relation", name="uq_capability_os_lineage_edge"
        ),
    )
    op.create_index(
        "ix_capability_os_lineage_child",
        "capability_os_lineage_edges",
        ["child_digest", "created_at_ms"],
    )

    op.create_table(
        "capability_os_policy_decisions",
        sa.Column("decision_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("workload_id", sa.Text(), nullable=False),
        sa.Column("artifact_digest", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("constraints", sa.JSON(), nullable=False),
        sa.Column("approval_id", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("decision_id"),
    )
    op.create_index(
        "ix_capability_os_policy_task_created",
        "capability_os_policy_decisions",
        ["task_id", "created_at_ms"],
    )
    op.create_index(
        "ix_capability_os_policy_artifact",
        "capability_os_policy_decisions",
        ["artifact_digest", "created_at_ms"],
    )

    op.create_table(
        "capability_os_observations",
        sa.Column("observation_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("artifact_digest", sa.Text(), nullable=True),
        sa.Column("evidence_id", sa.Text(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("observation_id"),
        sa.UniqueConstraint("evidence_id", name="uq_capability_os_observation_evidence"),
    )
    op.create_index(
        "ix_capability_os_observation_task",
        "capability_os_observations",
        ["task_id", "created_at_ms"],
    )
    op.create_index(
        "ix_capability_os_observation_run",
        "capability_os_observations",
        ["run_id", "created_at_ms"],
    )

    op.create_table(
        "capability_os_mcp_discovery_entries",
        sa.Column("entry_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("origin_uri", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("effects", sa.JSON(), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("refreshed_at_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("entry_id"),
    )
    op.create_index(
        "ix_capability_os_mcp_discovery_provider",
        "capability_os_mcp_discovery_entries",
        ["provider", "refreshed_at_ms"],
    )
    op.create_index(
        "ix_capability_os_mcp_discovery_server",
        "capability_os_mcp_discovery_entries",
        ["server_id", "version"],
    )

    op.create_table(
        "capability_os_retention_jobs",
        sa.Column("retention_job_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("artifact_digest", sa.Text(), nullable=True),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("scheduled_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("completed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("retention_job_id"),
    )
    op.create_index(
        "ix_capability_os_retention_status_schedule",
        "capability_os_retention_jobs",
        ["status", "scheduled_at_ms"],
    )
    op.create_index(
        "ix_capability_os_retention_artifact",
        "capability_os_retention_jobs",
        ["artifact_digest", "scheduled_at_ms"],
    )


def downgrade() -> None:
    op.drop_index("ix_capability_os_retention_artifact", table_name="capability_os_retention_jobs")
    op.drop_index(
        "ix_capability_os_retention_status_schedule", table_name="capability_os_retention_jobs"
    )
    op.drop_table("capability_os_retention_jobs")
    op.drop_index(
        "ix_capability_os_mcp_discovery_server", table_name="capability_os_mcp_discovery_entries"
    )
    op.drop_index(
        "ix_capability_os_mcp_discovery_provider", table_name="capability_os_mcp_discovery_entries"
    )
    op.drop_table("capability_os_mcp_discovery_entries")
    op.drop_index("ix_capability_os_observation_run", table_name="capability_os_observations")
    op.drop_index("ix_capability_os_observation_task", table_name="capability_os_observations")
    op.drop_table("capability_os_observations")
    op.drop_index("ix_capability_os_policy_artifact", table_name="capability_os_policy_decisions")
    op.drop_index(
        "ix_capability_os_policy_task_created", table_name="capability_os_policy_decisions"
    )
    op.drop_table("capability_os_policy_decisions")
    op.drop_index("ix_capability_os_lineage_child", table_name="capability_os_lineage_edges")
    op.drop_table("capability_os_lineage_edges")
    op.drop_index("ix_capability_os_promotion_experiment", table_name="capability_os_promotions")
    op.drop_table("capability_os_promotions")
    op.drop_index("ix_capability_os_experiment_run_arm", table_name="capability_os_experiment_runs")
    op.drop_table("capability_os_experiment_runs")
    op.drop_index("ix_capability_os_experiment_candidate", table_name="capability_os_experiments")
    op.drop_index("ix_capability_os_experiment_task_state", table_name="capability_os_experiments")
    op.drop_table("capability_os_experiments")
    op.drop_index("ix_capability_os_run_step_run_status", table_name="capability_os_run_steps")
    op.drop_table("capability_os_run_steps")
    op.drop_index("ix_capability_os_run_artifact_status", table_name="capability_os_runs")
    op.drop_index("ix_capability_os_run_task_started", table_name="capability_os_runs")
    op.drop_table("capability_os_runs")
