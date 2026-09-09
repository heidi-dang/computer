"""Add Capability OS experiments, promotions, lineage, and observations tables.

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
        "capability_os_experiments",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("baseline_version", sa.Text(), nullable=False),
        sa.Column("candidate_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("traffic_split", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=True),
        sa.Column("concluded_at", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_capability_os_experiment_artifact_status",
        "capability_os_experiments",
        ["artifact_id", "status"],
    )
    op.create_index(
        "ix_capability_os_experiment_owner",
        "capability_os_experiments",
        ["owner", "created_at"],
    )

    op.create_table(
        "capability_os_experiment_runs",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("success", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_capability_os_experiment_run_experiment",
        "capability_os_experiment_runs",
        ["experiment_id", "variant"],
    )
    op.create_index(
        "ix_capability_os_experiment_run_task",
        "capability_os_experiment_runs",
        ["task_id", "created_at"],
    )

    op.create_table(
        "capability_os_promotions",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("from_version", sa.Text(), nullable=False),
        sa.Column("to_version", sa.Text(), nullable=False),
        sa.Column("from_stage", sa.Text(), nullable=False),
        sa.Column("to_stage", sa.Text(), nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=True),
        sa.Column("promoted_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("policy_decision_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index(
        "ix_capability_os_promotion_artifact",
        "capability_os_promotions",
        ["artifact_id", "created_at"],
    )
    op.create_index(
        "ix_capability_os_promotion_stage",
        "capability_os_promotions",
        ["from_stage", "to_stage"],
    )

    op.create_table(
        "capability_os_lineage_edges",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=False),
        sa.Column("child_id", sa.Text(), nullable=False),
        sa.Column("edge_type", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index(
        "ix_capability_os_lineage_parent",
        "capability_os_lineage_edges",
        ["parent_id", "edge_type"],
    )
    op.create_index(
        "ix_capability_os_lineage_child",
        "capability_os_lineage_edges",
        ["child_id", "edge_type"],
    )

    op.create_table(
        "capability_os_policy_decisions",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("context_json", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index(
        "ix_capability_os_policy_decision_subject",
        "capability_os_policy_decisions",
        ["subject", "decision"],
    )
    op.create_index(
        "ix_capability_os_policy_decision_policy",
        "capability_os_policy_decisions",
        ["policy_id", "created_at"],
    )

    op.create_table(
        "capability_os_observations",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("artifact_version", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("metric_name", sa.Text(), nullable=False),
        sa.Column("metric_value", sa.Float(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("tags_json", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index(
        "ix_capability_os_observation_artifact",
        "capability_os_observations",
        ["artifact_id", "metric_name"],
    )
    op.create_index(
        "ix_capability_os_observation_task",
        "capability_os_observations",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("capability_os_observations")
    op.drop_table("capability_os_policy_decisions")
    op.drop_table("capability_os_lineage_edges")
    op.drop_table("capability_os_promotions")
    op.drop_table("capability_os_experiment_runs")
    op.drop_table("capability_os_experiments")
