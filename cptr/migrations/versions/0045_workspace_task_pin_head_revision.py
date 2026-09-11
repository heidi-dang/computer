"""Add head_revision column to workspace_task_repository_pins.

Revision ID: 0045
Revises: 0044
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # head_revision records the actual HEAD of the repo at pin/update time.
    # Existing rows get NULL (unknown); they will be populated on the next
    # pin_repository call for each task.
    op.add_column(
        "workspace_task_repository_pins",
        sa.Column("head_revision", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspace_task_repository_pins", "head_revision")
