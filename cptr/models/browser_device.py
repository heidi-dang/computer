"""Durable browser-device pairing, session, lease, and replay records."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


def _uuid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class BrowserDevice(Base):
    __tablename__ = "browser_devices"

    id = Column(Text, primary_key=True, default=lambda: _uuid("bdv"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(Text, nullable=False)
    credential_hash = Column(Text, nullable=False)
    credential_version = Column(BigInteger, nullable=False, default=1)
    status = Column(Text, nullable=False, default="ACTIVE")
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    last_seen_at = Column(BigInteger, nullable=True)
    revoked_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_browser_device_user_status", "user_id", "status", "updated_at"),
        Index("ix_browser_device_credential_hash", "credential_hash", unique=True),
    )


class BrowserPairingChallenge(Base):
    __tablename__ = "browser_pairing_challenges"

    id = Column(Text, primary_key=True, default=lambda: _uuid("pair"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    device_name = Column(Text, nullable=False)
    code_hash = Column(Text, nullable=False)
    claim_secret_hash = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="PENDING")
    created_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, nullable=False)
    approved_at = Column(BigInteger, nullable=True)
    claimed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_browser_pairing_status_expires", "status", "expires_at"),
        Index("ix_browser_pairing_code_hash", "code_hash"),
    )


class BrowserSession(Base):
    __tablename__ = "browser_sessions"

    id = Column(Text, primary_key=True, default=lambda: _uuid("brs"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id = Column(Text, ForeignKey("browser_devices.id", ondelete="CASCADE"), nullable=False)
    workbench_session_id = Column(
        Text, ForeignKey("workbench_sessions.id", ondelete="SET NULL"), nullable=True
    )
    tab_id = Column(BigInteger, nullable=False)
    state = Column(Text, nullable=False, default="OBSERVING")
    snapshot_id = Column(Text, nullable=True)
    surface_id = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    closed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_browser_session_user_device_state", "user_id", "device_id", "state"),
        Index("ix_browser_session_workbench", "workbench_session_id", "updated_at"),
    )


class BrowserLease(Base):
    __tablename__ = "browser_leases"

    id = Column(Text, primary_key=True, default=lambda: _uuid("brl"))
    device_id = Column(Text, ForeignKey("browser_devices.id", ondelete="CASCADE"), nullable=False)
    tab_id = Column(BigInteger, nullable=False)
    session_id = Column(Text, ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False)
    owner = Column(Text, nullable=False, default="none")
    epoch = Column(BigInteger, nullable=False, default=0)
    expires_at = Column(BigInteger, nullable=True)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("device_id", "tab_id", name="uq_browser_lease_device_tab"),
        Index("ix_browser_lease_session", "session_id"),
    )


class BrowserDeviceEvent(Base):
    __tablename__ = "browser_device_events"

    id = Column(Text, primary_key=True, default=lambda: _uuid("bde"))
    device_id = Column(Text, ForeignKey("browser_devices.id", ondelete="CASCADE"), nullable=False)
    sequence = Column(BigInteger, nullable=False)
    event_type = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("device_id", "sequence", name="uq_browser_device_event_sequence"),
        Index("ix_browser_device_event_device_sequence", "device_id", "sequence"),
    )
