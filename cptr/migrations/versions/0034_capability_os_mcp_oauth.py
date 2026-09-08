"""Add durable encrypted MCP OAuth flow and credential records.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capability_os_mcp_oauth_flows",
        sa.Column("flow_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("artifact_digest", sa.Text(), nullable=False),
        sa.Column("derived_artifact_digest", sa.Text(), nullable=False),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("remote_url", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("logical_name", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("state_hash", sa.Text(), nullable=False),
        sa.Column("code_verifier_encrypted", sa.Text(), nullable=False),
        sa.Column("client_info_encrypted", sa.Text(), nullable=False),
        sa.Column("protected_resource_metadata", sa.JSON(), nullable=True),
        sa.Column("oauth_metadata", sa.JSON(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("flow_id"),
        sa.UniqueConstraint("state_hash", name="uq_capability_os_mcp_oauth_state_hash"),
    )
    op.create_index(
        "ix_capability_os_mcp_oauth_flow_user_status",
        "capability_os_mcp_oauth_flows",
        ["user_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_capability_os_mcp_oauth_flow_expiry",
        "capability_os_mcp_oauth_flows",
        ["status", "expires_at_ms"],
        unique=False,
    )

    op.create_table(
        "capability_os_mcp_oauth_credentials",
        sa.Column("logical_name", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("remote_url", sa.Text(), nullable=False),
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("client_info_encrypted", sa.Text(), nullable=False),
        sa.Column("protected_resource_metadata", sa.JSON(), nullable=True),
        sa.Column("oauth_metadata", sa.JSON(), nullable=True),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("logical_name"),
        sa.UniqueConstraint(
            "user_id",
            "profile_id",
            "server_id",
            "remote_url",
            name="uq_capability_os_mcp_oauth_user_profile_remote",
        ),
    )
    op.create_index(
        "ix_capability_os_mcp_oauth_credential_user",
        "capability_os_mcp_oauth_credentials",
        ["user_id", "profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_capability_os_mcp_oauth_credential_remote",
        "capability_os_mcp_oauth_credentials",
        ["server_id", "remote_url"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_capability_os_mcp_oauth_credential_remote",
        table_name="capability_os_mcp_oauth_credentials",
    )
    op.drop_index(
        "ix_capability_os_mcp_oauth_credential_user",
        table_name="capability_os_mcp_oauth_credentials",
    )
    op.drop_table("capability_os_mcp_oauth_credentials")
    op.drop_index(
        "ix_capability_os_mcp_oauth_flow_expiry",
        table_name="capability_os_mcp_oauth_flows",
    )
    op.drop_index(
        "ix_capability_os_mcp_oauth_flow_user_status",
        table_name="capability_os_mcp_oauth_flows",
    )
    op.drop_table("capability_os_mcp_oauth_flows")
