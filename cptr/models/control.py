"""Persistent records for the CPTR control plane and autonomous monitors."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class ControlTask(Base):
    __tablename__ = "control_tasks"

    id = Column(Text, primary_key=True, default=_uuid)
    user_id = Column(Text, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Text, ForeignKey("workspaces.id"), nullable=False)
    chat_id = Column(Text, ForeignKey("chats.id"), nullable=False)
    message_id = Column(Text, ForeignKey("chat_messages.id"), nullable=False)
    status = Column(Text, nullable=False, default="PENDING")
    prompt = Column(Text, nullable=False)
    model_id = Column(Text, nullable=False)
    output = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    idempotency_key = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    cancelled_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_control_task_user_idempotency"),
        Index("ix_control_task_user_workspace", "user_id", "workspace_id"),
    )


class AutonomousMonitor(Base):
    __tablename__ = "autonomous_monitors"

    id = Column(Text, primary_key=True, default=_uuid)
    goal_id = Column(Text, nullable=False, unique=True)
    user_id = Column(Text, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Text, ForeignKey("workspaces.id"), nullable=False)
    original_goal = Column(Text, nullable=False)
    original_acceptance_criteria = Column(JSON, nullable=False)
    model_id = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="RUNNING")
    current_scope_id = Column(Text, nullable=True)
    approval_id = Column(Text, nullable=True)
    lock_token = Column(Text, nullable=True)
    lock_expires_at = Column(BigInteger, nullable=True)
    director_state = Column(JSON, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_autonomous_monitor_user_status", "user_id", "status"),)


class AutonomousScope(Base):
    __tablename__ = "autonomous_scopes"

    id = Column(Text, primary_key=True, default=_uuid)
    monitor_id = Column(
        Text, ForeignKey("autonomous_monitors.id", ondelete="CASCADE"), nullable=False
    )
    ordinal = Column(BigInteger, nullable=False)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    acceptance_criteria = Column(JSON, nullable=False)
    status = Column(Text, nullable=False, default="PENDING")
    attempt_count = Column(BigInteger, nullable=False, default=0)
    worker_task_ids = Column(JSON, nullable=False, default=list)
    verification_evidence = Column(JSON, nullable=False, default=list)
    failure_evidence = Column(JSON, nullable=False, default=list)
    last_decision = Column(JSON, nullable=False, default=dict)
    next_action = Column(Text, nullable=True)
    history = Column(JSON, nullable=False, default=list)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_autonomous_scope_monitor_ordinal", "monitor_id", "ordinal"),)


class AutonomousEvidence(Base):
    __tablename__ = "autonomous_evidence"

    id = Column(Text, primary_key=True, default=_uuid)
    monitor_id = Column(
        Text, ForeignKey("autonomous_monitors.id", ondelete="CASCADE"), nullable=False
    )
    scope_id = Column(Text, ForeignKey("autonomous_scopes.id", ondelete="CASCADE"), nullable=True)
    kind = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(BigInteger, nullable=False)


class AutonomousApproval(Base):
    __tablename__ = "autonomous_approvals"

    id = Column(Text, primary_key=True, default=_uuid)
    monitor_id = Column(
        Text, ForeignKey("autonomous_monitors.id", ondelete="CASCADE"), nullable=False
    )
    operation = Column(Text, nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="PENDING")
    requested_at = Column(BigInteger, nullable=False)
    decided_at = Column(BigInteger, nullable=True)
    decided_by = Column(Text, nullable=True)


class ControlIdempotency(Base):
    __tablename__ = "control_idempotency"

    id = Column(Text, primary_key=True, default=_uuid)
    user_id = Column(Text, ForeignKey("users.id"), nullable=False)
    key = Column(Text, nullable=False)
    resource_type = Column(Text, nullable=False)
    resource_id = Column(Text, nullable=False)
    response = Column(JSON, nullable=True)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_control_idempotency_user_key"),)
