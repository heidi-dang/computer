"""Persist the worker setup boundary for queued control delivery."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "control_messages",
        sa.Column("setup_readiness_status", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("control_messages", "setup_readiness_status")
