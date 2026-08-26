"""Persist idempotency for direct-operation approval and cancellation requests.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "direct_operation_requests",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "operation_id",
            sa.Text(),
            sa.ForeignKey("direct_operations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_type", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "operation_id",
            "request_type",
            "idempotency_key",
            name="uq_direct_operation_request_idempotency",
        ),
    )


def downgrade() -> None:
    op.drop_table("direct_operation_requests")
