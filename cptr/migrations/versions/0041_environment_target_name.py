"""Add target_name column to environment_profiles.

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # target_name is nullable to allow un-anchored profiles (no default target).
    # Existing rows receive NULL, meaning they are un-anchored until explicitly set.
    op.add_column(
        "environment_profiles",
        sa.Column("target_name", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("environment_profiles", "target_name")
