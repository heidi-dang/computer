"""Track canonical-memory provenance on derived entities.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_records",
        sa.Column("observed_at_ms", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.execute("UPDATE memory_records SET observed_at_ms = created_at_ms WHERE observed_at_ms = 0")
    op.add_column(
        "memory_records",
        sa.Column("superseded_at_ms", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "memory_entities",
        sa.Column("source_memory_ids", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("memory_entities", "source_memory_ids")
    op.drop_column("memory_records", "superseded_at_ms")
    op.drop_column("memory_records", "observed_at_ms")
