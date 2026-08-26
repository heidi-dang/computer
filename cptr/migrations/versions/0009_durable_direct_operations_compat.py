"""Backfill durable direct-operation tables for legacy revision 0008 deployments.

Some existing CPTR data directories were upgraded by an earlier revision named
0008 before this repository branch introduced durable direct operations. This
idempotent compatibility revision guarantees the required tables exist whether
0008 was the legacy revision or the direct-operations migration.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("direct_operations"):
        op.create_table(
            "direct_operations",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column(
                "workspace_id",
                sa.Text(),
                sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False, server_default="REQUESTED"),
            sa.Column("request", sa.JSON(), nullable=False),
            sa.Column("request_digest", sa.Text(), nullable=False),
            sa.Column("idempotency_key", sa.Text(), nullable=False),
            sa.Column("expected_revision", sa.Text(), nullable=True),
            sa.Column("lease_fencing_token", sa.BigInteger(), nullable=True),
            sa.Column("approval_id", sa.Text(), nullable=True),
            sa.Column("executor_type", sa.Text(), nullable=True),
            sa.Column("executor_ref", sa.Text(), nullable=True),
            sa.Column("public_result", sa.JSON(), nullable=True),
            sa.Column("public_error_code", sa.Text(), nullable=True),
            sa.Column("cancel_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
            sa.Column("started_at", sa.BigInteger(), nullable=True),
            sa.Column("finished_at", sa.BigInteger(), nullable=True),
            sa.Column("cancel_requested_at", sa.BigInteger(), nullable=True),
            sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
            sa.UniqueConstraint(
                "user_id",
                "workspace_id",
                "kind",
                "idempotency_key",
                name="uq_direct_operation_idempotency",
            ),
        )
        op.create_index(
            "ix_direct_operation_workspace_state", "direct_operations", ["workspace_id", "state"]
        )
        op.create_index(
            "ix_direct_operation_user_created", "direct_operations", ["user_id", "created_at"]
        )
        op.create_index("ix_direct_operation_executor_ref", "direct_operations", ["executor_ref"])

    if not _has_table("direct_operation_events"):
        op.create_table(
            "direct_operation_events",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column(
                "operation_id",
                sa.Text(),
                sa.ForeignKey("direct_operations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("event_type", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
        )
        op.create_index(
            "ix_direct_operation_event_operation",
            "direct_operation_events",
            ["operation_id", "created_at"],
        )

    if not _has_table("direct_operation_approvals"):
        op.create_table(
            "direct_operation_approvals",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column(
                "operation_id",
                sa.Text(),
                sa.ForeignKey("direct_operations.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("request_digest", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
            sa.Column("requested_at", sa.BigInteger(), nullable=False),
            sa.Column("expires_at", sa.BigInteger(), nullable=True),
            sa.Column("decided_at", sa.BigInteger(), nullable=True),
            sa.Column("decided_by", sa.Text(), nullable=True),
        )

    if not _has_table("workspace_operation_leases"):
        op.create_table(
            "workspace_operation_leases",
            sa.Column(
                "workspace_id",
                sa.Text(),
                sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("holder_type", sa.Text(), nullable=False),
            sa.Column("holder_id", sa.Text(), nullable=False),
            sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("acquired_at", sa.BigInteger(), nullable=False),
            sa.Column("expires_at", sa.BigInteger(), nullable=False),
        )


def downgrade() -> None:
    # Tables may have been created by revision 0008, so do not drop shared
    # durable-operation tables during a compatibility-only downgrade.
    pass
