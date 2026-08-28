"""Add indexed control API keys.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "control_api_keys",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False, server_default="default"),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_control_api_key_hash", "control_api_keys", ["key_hash"], unique=True)
    op.create_index(
        "ix_control_api_key_user_created", "control_api_keys", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_control_api_key_user_created", table_name="control_api_keys")
    op.drop_index("ix_control_api_key_hash", table_name="control_api_keys")
    op.drop_table("control_api_keys")
