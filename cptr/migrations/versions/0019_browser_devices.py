"""Add durable real-Chrome browser device broker records.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_devices",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("credential_hash", sa.Text(), nullable=False),
        sa.Column("credential_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_at", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_browser_device_user_status", "browser_devices", ["user_id", "status", "updated_at"])
    op.create_index("ix_browser_device_credential_hash", "browser_devices", ["credential_hash"], unique=True)

    op.create_table(
        "browser_pairing_challenges",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("device_name", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("claim_secret_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("approved_at", sa.BigInteger(), nullable=True),
        sa.Column("claimed_at", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_browser_pairing_status_expires", "browser_pairing_challenges", ["status", "expires_at"])
    op.create_index("ix_browser_pairing_code_hash", "browser_pairing_challenges", ["code_hash"])

    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.Text(), sa.ForeignKey("browser_devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workbench_session_id", sa.Text(), sa.ForeignKey("workbench_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tab_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="OBSERVING"),
        sa.Column("snapshot_id", sa.Text(), nullable=True),
        sa.Column("surface_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("closed_at", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_browser_session_user_device_state", "browser_sessions", ["user_id", "device_id", "state"])
    op.create_index("ix_browser_session_workbench", "browser_sessions", ["workbench_session_id", "updated_at"])

    op.create_table(
        "browser_leases",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("device_id", sa.Text(), sa.ForeignKey("browser_devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tab_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.Text(), sa.ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False, server_default="none"),
        sa.Column("epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("device_id", "tab_id", name="uq_browser_lease_device_tab"),
    )
    op.create_index("ix_browser_lease_session", "browser_leases", ["session_id"])

    op.create_table(
        "browser_device_events",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("device_id", sa.Text(), sa.ForeignKey("browser_devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("device_id", "sequence", name="uq_browser_device_event_sequence"),
    )
    op.create_index("ix_browser_device_event_device_sequence", "browser_device_events", ["device_id", "sequence"])


def downgrade() -> None:
    op.drop_index("ix_browser_device_event_device_sequence", table_name="browser_device_events")
    op.drop_table("browser_device_events")
    op.drop_index("ix_browser_lease_session", table_name="browser_leases")
    op.drop_table("browser_leases")
    op.drop_index("ix_browser_session_workbench", table_name="browser_sessions")
    op.drop_index("ix_browser_session_user_device_state", table_name="browser_sessions")
    op.drop_table("browser_sessions")
    op.drop_index("ix_browser_pairing_code_hash", table_name="browser_pairing_challenges")
    op.drop_index("ix_browser_pairing_status_expires", table_name="browser_pairing_challenges")
    op.drop_table("browser_pairing_challenges")
    op.drop_index("ix_browser_device_credential_hash", table_name="browser_devices")
    op.drop_index("ix_browser_device_user_status", table_name="browser_devices")
    op.drop_table("browser_devices")
