"""Owner-scoped persistent Guard Control overrides and audit events."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Index, Text

from cptr.models.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class GuardSetting(Base):
    """One owner-scoped override for a code-defined mutable guard."""

    __tablename__ = "guard_settings"

    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    guard_id = Column(Text, primary_key=True)
    enabled = Column(Boolean, nullable=False)
    version = Column(BigInteger, nullable=False, default=1)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_guard_settings_user_updated", "user_id", "updated_at"),)


class GuardSettingEvent(Base):
    """Append-only audit record for an owner Guard Control mutation."""

    __tablename__ = "guard_setting_events"

    event_id = Column(Text, primary_key=True, default=_uuid)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    guard_id = Column(Text, nullable=False)
    previous_enabled = Column(Boolean, nullable=False)
    new_enabled = Column(Boolean, nullable=False)
    version = Column(BigInteger, nullable=False)
    source = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_guard_setting_events_user_created", "user_id", "created_at"),
        Index("ix_guard_setting_events_guard_created", "guard_id", "created_at"),
    )
