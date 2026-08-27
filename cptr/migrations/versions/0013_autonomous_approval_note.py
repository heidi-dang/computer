"""Add optional notes to durable autonomous approval decisions.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("autonomous_approvals", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("autonomous_approvals", "note")
