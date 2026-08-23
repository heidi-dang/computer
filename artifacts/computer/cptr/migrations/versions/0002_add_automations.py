"""add_automations + rename chat/chat_message to plural

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Rename existing tables to plural ──
    op.rename_table('chat', 'chats')
    op.rename_table('chat_message', 'chat_messages')

    # ── Create automation tables ──
    op.create_table('automations',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('model_id', sa.Text(), nullable=False),
        sa.Column('workspace', sa.Text(), nullable=False),
        sa.Column('rrule', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('last_run_at', sa.BigInteger(), nullable=True),
        sa.Column('next_run_at', sa.BigInteger(), nullable=True),
        sa.Column('meta', sqlite.JSON(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_automation_next_run', 'automations', ['next_run_at'], unique=False)

    op.create_table('automation_runs',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('automation_id', sa.Text(), nullable=False),
        sa.Column('chat_id', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_automation_run_aid_created', 'automation_runs', ['automation_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_automation_runs_automation_id'), 'automation_runs', ['automation_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_automation_next_run', table_name='automations')
    op.drop_table('automations')
    op.drop_index(op.f('ix_automation_runs_automation_id'), table_name='automation_runs')
    op.drop_index('ix_automation_run_aid_created', table_name='automation_runs')
    op.drop_table('automation_runs')

    op.rename_table('chats', 'chat')
    op.rename_table('chat_messages', 'chat_message')
