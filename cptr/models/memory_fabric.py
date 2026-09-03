"""Durable, owner-scoped observability events for the CPTR memory fabric."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Text
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


def _event_id() -> str:
    return f"mem_evt_{uuid.uuid4().hex}"


class MemoryFabricEvent(Base):
    """Immutable metadata/provenance event emitted by managed-memory recall and writes."""

    __tablename__ = "memory_fabric_events"

    id = Column(Text, primary_key=True, default=_event_id)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=True)
    event_type = Column(Text, nullable=False)
    scope = Column(Text, nullable=True)
    memory_id = Column(Text, nullable=True)
    path = Column(Text, nullable=True)
    heading = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    trust_level = Column(Text, nullable=False, default="managed_memory")
    confidence_ppm = Column(BigInteger, nullable=False, default=1_000_000)
    payload = Column(JSON, nullable=False, default=dict)
    created_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_memory_fabric_user_created", "user_id", "created_at_ms"),
        Index("ix_memory_fabric_user_workspace_created", "user_id", "workspace", "created_at_ms"),
        Index("ix_memory_fabric_user_type_created", "user_id", "event_type", "created_at_ms"),
    )
