"""Durable, owner-scoped memory for direct CPTR workspace work.

The immutable event ledger deliberately stores only redacted, bounded activity
summaries.  It is distinct from native-chat Markdown memory and never stores a
ChatGPT private prompt, chain-of-thought, credential, or raw terminal output.
"""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


def _memory_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _stream_id() -> str:
    return _memory_id("wmm")


def _event_id() -> str:
    return _memory_id("wme")


def _fact_id() -> str:
    return _memory_id("wmf")


class WorkspaceMemoryStream(Base):
    """One durable event stream and compact current-state snapshot per owner/workspace."""

    __tablename__ = "workspace_memory_streams"

    id = Column(Text, primary_key=True, default=_stream_id)
    user_id = Column(Text, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    next_sequence = Column(BigInteger, nullable=False, default=0)
    snapshot = Column(JSON, nullable=False, default=dict)
    snapshot_through_sequence = Column(BigInteger, nullable=False, default=0)
    workspace_fingerprint = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "workspace_id", name="uq_workspace_memory_stream_owner_workspace"),
        Index("ix_workspace_memory_stream_owner_updated", "user_id", "updated_at"),
    )


class WorkspaceMemoryEvent(Base):
    """Redacted immutable direct-work activity event ordered within a memory stream."""

    __tablename__ = "workspace_memory_events"

    id = Column(Text, primary_key=True, default=_event_id)
    stream_id = Column(
        Text, ForeignKey("workspace_memory_streams.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(Text, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    sequence = Column(BigInteger, nullable=False)
    operation_id = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)
    source = Column(Text, nullable=False, default="mcp")
    session_id = Column(Text, ForeignKey("workbench_sessions.id", ondelete="SET NULL"), nullable=True)
    tool_name = Column(Text, nullable=True)
    outcome = Column(Text, nullable=False, default="COMPLETE")
    summary = Column(Text, nullable=False)
    affected_paths = Column(JSON, nullable=False, default=list)
    details = Column(JSON, nullable=False, default=dict)
    workspace_fingerprint = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("stream_id", "sequence", name="uq_workspace_memory_event_stream_sequence"),
        UniqueConstraint(
            "user_id", "workspace_id", "operation_id", "kind", name="uq_workspace_memory_event_operation"
        ),
        Index("ix_workspace_memory_event_owner_workspace_sequence", "user_id", "workspace_id", "sequence"),
        Index("ix_workspace_memory_event_session", "session_id", "sequence"),
    )


class WorkspaceMemoryFact(Base):
    """Explicitly curated durable workspace knowledge with source provenance."""

    __tablename__ = "workspace_memory_facts"

    id = Column(Text, primary_key=True, default=_fact_id)
    user_id = Column(Text, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    category = Column(Text, nullable=False, default="note")
    content = Column(Text, nullable=False)
    paths = Column(JSON, nullable=False, default=list)
    source_event_id = Column(Text, ForeignKey("workspace_memory_events.id", ondelete="SET NULL"), nullable=True)
    verified_fingerprint = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="ACTIVE")
    pinned = Column(Boolean, nullable=False, default=False)
    revision = Column(BigInteger, nullable=False, default=1)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    deleted_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_workspace_memory_fact_owner_workspace_status", "user_id", "workspace_id", "status"),
        Index("ix_workspace_memory_fact_owner_workspace_updated", "user_id", "workspace_id", "updated_at"),
    )


__all__ = [
    "WorkspaceMemoryEvent",
    "WorkspaceMemoryFact",
    "WorkspaceMemoryStream",
]
