"""Add Capability OS artefact, lease, evidence, and MCP mount persistence.

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capability_os_artifacts",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("parent_ref", sa.Text(), nullable=True),
        sa.Column("task_origin", sa.Text(), nullable=True),
        sa.Column("source_digest", sa.Text(), nullable=True),
        sa.Column("content_digest", sa.Text(), nullable=False),
        sa.Column("compatibility", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("spec", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("retired_at_ms", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint(
            "artifact_id",
            "version",
            "content_digest",
            name="uq_capability_os_artifact_immutable_identity",
        ),
        sa.UniqueConstraint(
            "content_digest",
            name="uq_capability_os_artifact_content_digest",
        ),
    )
    op.create_index(
        "ix_capability_os_artifact_kind_state",
        "capability_os_artifacts",
        ["kind", "state"],
    )
    op.create_index(
        "ix_capability_os_artifact_user_state",
        "capability_os_artifacts",
        ["user_id", "state"],
    )
    op.create_index(
        "ix_capability_os_artifact_identity",
        "capability_os_artifacts",
        ["artifact_id", "version"],
    )
    op.create_index(
        "ix_capability_os_artifact_task",
        "capability_os_artifacts",
        ["task_origin", "created_at_ms"],
    )

    op.create_table(
        "capability_os_leases",
        sa.Column("lease_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("workload_id", sa.Text(), nullable=False),
        sa.Column("artifact_digest", sa.Text(), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("resource_limits", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("network", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("credentials", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("approval_id", sa.Text(), nullable=True),
        sa.Column(
            "parent_lease_id",
            sa.Text(),
            sa.ForeignKey("capability_os_leases.lease_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("policy_decision_id", sa.Text(), nullable=False),
        sa.Column("attestation_requirements", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("runtime_profile", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("issued_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at_ms", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_capability_os_lease_task_status",
        "capability_os_leases",
        ["task_id", "status"],
    )
    op.create_index(
        "ix_capability_os_lease_artifact",
        "capability_os_leases",
        ["artifact_digest", "status"],
    )
    op.create_index(
        "ix_capability_os_lease_expiry",
        "capability_os_leases",
        ["status", "expires_at_ms"],
    )

    op.create_table(
        "capability_os_evidence",
        sa.Column("evidence_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("producer_identity", sa.Text(), nullable=False),
        sa.Column("claims", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("blob_uri", sa.Text(), nullable=True),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("artifact_digest", sa.Text(), nullable=True),
        sa.Column(
            "lease_id",
            sa.Text(),
            sa.ForeignKey("capability_os_leases.lease_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_capability_os_evidence_task_created",
        "capability_os_evidence",
        ["task_id", "created_at_ms"],
    )
    op.create_index(
        "ix_capability_os_evidence_artifact",
        "capability_os_evidence",
        ["artifact_digest", "created_at_ms"],
    )
    op.create_index(
        "ix_capability_os_evidence_lease",
        "capability_os_evidence",
        ["lease_id", "created_at_ms"],
    )

    op.create_table(
        "capability_os_mcp_mounts",
        sa.Column("mount_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "lease_id",
            sa.Text(),
            sa.ForeignKey("capability_os_leases.lease_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("projected_tools", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("transport_kind", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("released_at_ms", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_capability_os_mcp_mount_task_state",
        "capability_os_mcp_mounts",
        ["task_id", "state"],
    )
    op.create_index(
        "ix_capability_os_mcp_mount_server",
        "capability_os_mcp_mounts",
        ["server_id", "version", "digest"],
    )


def downgrade() -> None:
    op.drop_index("ix_capability_os_mcp_mount_server", table_name="capability_os_mcp_mounts")
    op.drop_index("ix_capability_os_mcp_mount_task_state", table_name="capability_os_mcp_mounts")
    op.drop_table("capability_os_mcp_mounts")
    op.drop_index("ix_capability_os_evidence_lease", table_name="capability_os_evidence")
    op.drop_index("ix_capability_os_evidence_artifact", table_name="capability_os_evidence")
    op.drop_index("ix_capability_os_evidence_task_created", table_name="capability_os_evidence")
    op.drop_table("capability_os_evidence")
    op.drop_index("ix_capability_os_lease_expiry", table_name="capability_os_leases")
    op.drop_index("ix_capability_os_lease_artifact", table_name="capability_os_leases")
    op.drop_index("ix_capability_os_lease_task_status", table_name="capability_os_leases")
    op.drop_table("capability_os_leases")
    op.drop_index("ix_capability_os_artifact_task", table_name="capability_os_artifacts")
    op.drop_index("ix_capability_os_artifact_identity", table_name="capability_os_artifacts")
    op.drop_index("ix_capability_os_artifact_user_state", table_name="capability_os_artifacts")
    op.drop_index("ix_capability_os_artifact_kind_state", table_name="capability_os_artifacts")
    op.drop_table("capability_os_artifacts")
