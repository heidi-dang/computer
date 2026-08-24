"""Durable FlowDeck orchestration state.

These records are lifecycle state and evidence envelopes only. They do not
execute providers, tools, commands, agents, or workspace mutations.
"""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Column, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class FlowDeckRun(Base):
    __tablename__ = "flowdeck_runs"

    id = Column(Text, primary_key=True, default=_uuid)
    request_key = Column(Text, nullable=False, unique=True)
    workspace = Column(Text, nullable=True)
    owner = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="PENDING")
    heartbeat_at = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (Index("ix_flowdeck_runs_status_heartbeat", "status", "heartbeat_at"),)


class FlowDeckStep(Base):
    __tablename__ = "flowdeck_steps"

    id = Column(Text, primary_key=True, default=_uuid)
    run_id = Column(Text, nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    name = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="PENDING")
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_flowdeck_step_sequence"),)


class FlowDeckBuildNode(Base):
    """Durable DAG metadata; execution remains owned by native CPTR."""

    __tablename__ = "flowdeck_build_nodes"

    id = Column(Text, primary_key=True, default=_uuid)
    run_id = Column(Text, nullable=False, index=True)
    node_key = Column(Text, nullable=False)
    dependencies = Column(JSON, nullable=False, default=list)
    mutation = Column(Integer, nullable=False, default=0)
    workspace = Column(Text, nullable=False)
    worktree = Column(Text, nullable=True)
    common_base = Column(Text, nullable=True)
    overlap_paths = Column(JSON, nullable=False, default=list)
    status = Column(Text, nullable=False, default="PENDING")
    attempt = Column(Integer, nullable=False, default=0)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (UniqueConstraint("run_id", "node_key", name="uq_flowdeck_build_node_key"),)


class FlowDeckLogicalOperation(Base):
    __tablename__ = "flowdeck_logical_operations"

    id = Column(Text, primary_key=True, default=_uuid)
    run_id = Column(Text, nullable=False, index=True)
    step_id = Column(Text, nullable=True, index=True)
    idempotency_key = Column(Text, nullable=False, unique=True)
    capability = Column(Text, nullable=False)
    target = Column(Text, nullable=False)
    reconcile_kind = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="INTENT_RECORDED")
    intent_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    outcome = Column(Text, nullable=True)
    authoritative_evidence = Column(JSON, nullable=True)


class FlowDeckPhysicalAttempt(Base):
    __tablename__ = "flowdeck_physical_attempts"

    id = Column(Text, primary_key=True, default=_uuid)
    operation_id = Column(Text, nullable=False, index=True)
    attempt_no = Column(Integer, nullable=False)
    status = Column(Text, nullable=False, default="PREPARED")
    fencing_epoch = Column(Integer, nullable=False)
    started_at = Column(BigInteger, nullable=False)
    heartbeat_at = Column(BigInteger, nullable=True)
    ended_at = Column(BigInteger, nullable=True)
    outcome = Column(Text, nullable=True)
    error = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("operation_id", "attempt_no", name="uq_flowdeck_attempt_number"),
    )


class FlowDeckEvent(Base):
    __tablename__ = "flowdeck_events"

    id = Column(Text, primary_key=True, default=_uuid)
    run_id = Column(Text, nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    kind = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_flowdeck_event_sequence"),)


class FlowDeckWorkspaceLease(Base):
    __tablename__ = "flowdeck_workspace_leases"

    workspace = Column(Text, primary_key=True)
    run_id = Column(Text, nullable=False)
    owner = Column(Text, nullable=False)
    epoch = Column(Integer, nullable=False)
    acquired_at = Column(BigInteger, nullable=False)
    heartbeat_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, nullable=False)


class FlowDeckRecoveryLease(Base):
    __tablename__ = "flowdeck_recovery_leases"

    run_id = Column(Text, primary_key=True)
    owner = Column(Text, nullable=False)
    epoch = Column(Integer, nullable=False)
    acquired_at = Column(BigInteger, nullable=False)
    heartbeat_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, nullable=False)


class FlowDeckApproval(Base):
    __tablename__ = "flowdeck_approvals"

    id = Column(Text, primary_key=True, default=_uuid)
    run_id = Column(Text, nullable=False, index=True)
    operation_id = Column(Text, nullable=False, unique=True)
    capability = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="PENDING")
    requested_at = Column(BigInteger, nullable=False)
    resolved_at = Column(BigInteger, nullable=True)
    resolved_by = Column(Text, nullable=True)
    evidence = Column(JSON, nullable=True)

    __table_args__ = (Index("ix_flowdeck_approvals_status", "status"),)