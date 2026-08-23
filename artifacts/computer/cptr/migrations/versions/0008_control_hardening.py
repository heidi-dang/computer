"""Harden autonomous evidence, retry signatures, and workspace coordination.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "autonomous_scopes",
        sa.Column("failure_signature_counts", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "autonomous_monitors",
        sa.Column("approved_operations", sa.JSON(), nullable=True),
    )
    op.create_table(
        "autonomous_workspace_leases",
        sa.Column(
            "workspace_id",
            sa.Text(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "monitor_id",
            sa.Text(),
            sa.ForeignKey("autonomous_monitors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lock_token", sa.Text(), nullable=False),
        sa.Column("acquired_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("autonomous_workspace_leases")
    op.drop_column("autonomous_monitors", "approved_operations")
    op.drop_column("autonomous_scopes", "failure_signature_counts")
