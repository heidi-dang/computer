"""Add durable outcome evidence for normal task steering.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("control_messages", sa.Column("effect_status", sa.Text(), nullable=True))
    op.add_column("control_messages", sa.Column("effect_evidence", sa.JSON(), nullable=True))
    op.add_column("control_messages", sa.Column("effect_observed_at", sa.BigInteger(), nullable=True))
    # Historical rows lack target-bound effect evidence. Preserve transport state
    # but never retroactively label a consumed instruction as successful.
    op.execute(
        "UPDATE control_messages SET effect_status = "
        "CASE WHEN status = 'CONSUMED' THEN 'EFFECT_NOT_OBSERVED' "
        "WHEN status = 'CANCELLED' THEN 'DELIVERY_FAILED' "
        "ELSE 'PENDING_DELIVERY' END "
        "WHERE effect_status IS NULL"
    )


def downgrade() -> None:
    op.drop_column("control_messages", "effect_observed_at")
    op.drop_column("control_messages", "effect_evidence")
    op.drop_column("control_messages", "effect_status")
