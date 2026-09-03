"""Add Dark Factory capability manifests and performance memory.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "factory_capabilities",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("stable_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("origin_type", sa.Text(), nullable=False),
        sa.Column("origin_uri", sa.Text(), nullable=False),
        sa.Column("pinned_version_or_commit", sa.Text(), nullable=True),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("permissions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("network_requirements", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("execution_requirements", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("risk_classification", sa.Text(), nullable=False),
        sa.Column("trust_status", sa.Text(), nullable=False),
        sa.Column("verification_status", sa.Text(), nullable=False),
        sa.Column("maintenance_metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("historical_factory_score_ppm", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("evaluated_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint(
            "stable_id",
            "version",
            "digest",
            name="uq_factory_capability_immutable_identity",
        ),
    )
    op.create_index(
        "ix_factory_capability_stable_version",
        "factory_capabilities",
        ["stable_id", "version"],
    )
    op.create_index(
        "ix_factory_capability_trust",
        "factory_capabilities",
        ["trust_status", "verification_status"],
    )

    op.create_table(
        "factory_capability_performance",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "capability_id",
            sa.Text(),
            sa.ForeignKey("factory_capabilities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repository_family", sa.Text(), nullable=False),
        sa.Column("task_family", sa.Text(), nullable=False),
        sa.Column("attempts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("verified_successes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("verified_failures", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("regressions", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("repair_iterations", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("runtime_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("confidence_ppm", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "capability_id",
            "repository_family",
            "task_family",
            name="uq_factory_capability_performance_family",
        ),
    )
    op.create_index(
        "ix_factory_capability_performance_lookup",
        "factory_capability_performance",
        ["capability_id", "repository_family", "task_family"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_factory_capability_performance_lookup",
        table_name="factory_capability_performance",
    )
    op.drop_table("factory_capability_performance")
    op.drop_index("ix_factory_capability_trust", table_name="factory_capabilities")
    op.drop_index("ix_factory_capability_stable_version", table_name="factory_capabilities")
    op.drop_table("factory_capabilities")
