"""Add chat_summary to chat_messages for context compaction.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('chat_messages', sa.Column('chat_summary', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('chat_messages', 'chat_summary')
