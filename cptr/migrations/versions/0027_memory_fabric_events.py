"""Add durable managed-memory recall and mutation provenance events.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_fabric_events",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("memory_id", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("heading", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("trust_level", sa.Text(), nullable=False, server_default="managed_memory"),
        sa.Column("confidence_ppm", sa.BigInteger(), nullable=False, server_default="1000000"),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_memory_fabric_user_created",
        "memory_fabric_events",
        ["user_id", "created_at_ms"],
    )
    op.create_index(
        "ix_memory_fabric_user_workspace_created",
        "memory_fabric_events",
        ["user_id", "workspace", "created_at_ms"],
    )
    op.create_index(
        "ix_memory_fabric_user_type_created",
        "memory_fabric_events",
        ["user_id", "event_type", "created_at_ms"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_fabric_user_type_created", table_name="memory_fabric_events")
    op.drop_index("ix_memory_fabric_user_workspace_created", table_name="memory_fabric_events")
    op.drop_index("ix_memory_fabric_user_created", table_name="memory_fabric_events")
    op.drop_table("memory_fabric_events")
