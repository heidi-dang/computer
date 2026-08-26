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
    review_status = Column(Text, nullable=False, default="NOT_REQUIRED")
    review_summary = Column(JSON, nullable=True)
    review_decision = Column(JSON, nullable=True)
    review_ready_at = Column(BigInteger, nullable=True)
    reviewed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_control_task_user_idempotency"),
        Index("ix_control_task_user_workspace", "user_id", "workspace_id"),
    )


class ControlMessage(Base):
    """Durable follow-up delivery record for task and autonomous steering."""

    __tablename__ = "control_messages"

    id = Column(Text, primary_key=True, default=_uuid)
    user_id = Column(Text, ForeignKey("users.id"), nullable=False)
    task_id = Column(Text, ForeignKey("control_tasks.id", ondelete="CASCADE"), nullable=False)
    chat_id = Column(Text, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    chat_message_id = Column(Text, ForeignKey("chat_messages.id"), nullable=True)
    content = Column(Text, nullable=False)
    dedupe_key = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="QUEUED")
    setup_readiness_status = Column(Text, nullable=True)
    target_message_id = Column(Text, nullable=True)
    monitor_id = Column(Text, nullable=True)
    scope_id = Column(Text, nullable=True)
    intended_message_id = Column(Text, nullable=True)
    consumed_task_id = Column(Text, nullable=True)
    consumed_message_id = Column(Text, nullable=True)
    # Delivery/consumption only prove handoff. A normal task steering request
    # must retain a separate, fail-closed outcome until the continuation can
    # provide target-bound evidence of the requested effect.
    effect_status = Column(Text, nullable=True)
    effect_evidence = Column(JSON, nullable=True)
    effect_observed_at = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    delivered_at = Column(BigInteger, nullable=True)
    consumed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "task_id", "dedupe_key", name="uq_control_message_dedupe"),
        Index("ix_control_message_chat_status", "chat_id", "status"),
        Index("ix_control_message_monitor_scope", "monitor_id", "scope_id", "status"),
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
    approved_operations = Column(JSON, nullable=True)
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
    steering_requests = Column(JSON, nullable=False, default=list)
    verification_evidence = Column(JSON, nullable=False, default=list)
    failure_evidence = Column(JSON, nullable=False, default=list)
    failure_signature_counts = Column(JSON, nullable=False, default=dict)
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


class AutonomousWorkspaceLease(Base):
    __tablename__ = "autonomous_workspace_leases"

    workspace_id = Column(Text, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    monitor_id = Column(
        Text, ForeignKey("autonomous_monitors.id", ondelete="CASCADE"), nullable=False
    )
    lock_token = Column(Text, nullable=False)
    acquired_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, nullable=False)


class DirectOperation(Base):
    """Durable owner for an agent-free direct workspace operation."""

    __tablename__ = "direct_operations"

    id = Column(Text, primary_key=True, default=_uuid)
    user_id = Column(Text, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    kind = Column(Text, nullable=False)
    state = Column(Text, nullable=False, default="REQUESTED")
    request = Column(JSON, nullable=False, default=dict)
    request_digest = Column(Text, nullable=False)
    idempotency_key = Column(Text, nullable=False)
    expected_revision = Column(Text, nullable=True)
    lease_fencing_token = Column(BigInteger, nullable=True)
    approval_id = Column(Text, nullable=True)
    executor_type = Column(Text, nullable=True)
    executor_ref = Column(Text, nullable=True)
    public_result = Column(JSON, nullable=True)
    public_error_code = Column(Text, nullable=True)
    cancel_reason = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    started_at = Column(BigInteger, nullable=True)
    finished_at = Column(BigInteger, nullable=True)
    cancel_requested_at = Column(BigInteger, nullable=True)
    version = Column(BigInteger, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "workspace_id",
            "kind",
            "idempotency_key",
            name="uq_direct_operation_idempotency",
        ),
        Index("ix_direct_operation_workspace_state", "workspace_id", "state"),
        Index("ix_direct_operation_user_created", "user_id", "created_at"),
        Index("ix_direct_operation_executor_ref", "executor_ref"),
    )


class DirectOperationEvent(Base):
    """Append-only public-safe lifecycle evidence for a direct operation."""

    __tablename__ = "direct_operation_events"

    id = Column(Text, primary_key=True, default=_uuid)
    operation_id = Column(
        Text, ForeignKey("direct_operations.id", ondelete="CASCADE"), nullable=False
    )
    event_type = Column(Text, nullable=False)
    state = Column(Text, nullable=True)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_direct_operation_event_operation", "operation_id", "created_at"),)


class DirectOperationApproval(Base):
    """Operation-specific approval that cannot be forged by an execution request flag."""

    __tablename__ = "direct_operation_approvals"

    id = Column(Text, primary_key=True, default=_uuid)
    operation_id = Column(
        Text, ForeignKey("direct_operations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    request_digest = Column(Text, nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="PENDING")
    requested_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, nullable=True)
    decided_at = Column(BigInteger, nullable=True)
    decided_by = Column(Text, nullable=True)


class DirectOperationRequest(Base):
    """Exactly-once record for a state-changing operation subrequest."""

    __tablename__ = "direct_operation_requests"

    id = Column(Text, primary_key=True, default=_uuid)
    operation_id = Column(
        Text, ForeignKey("direct_operations.id", ondelete="CASCADE"), nullable=False
    )
    request_type = Column(Text, nullable=False)
    idempotency_key = Column(Text, nullable=False)
    request_digest = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            "request_type",
            "idempotency_key",
            name="uq_direct_operation_request_idempotency",
        ),
    )


class WorkspaceOperationLease(Base):
    """A generalized fenced workspace mutation lease shared by monitors and direct operations."""

    __tablename__ = "workspace_operation_leases"

    workspace_id = Column(Text, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    holder_type = Column(Text, nullable=False)
    holder_id = Column(Text, nullable=False)
    fencing_token = Column(BigInteger, nullable=False, default=0)
    acquired_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, nullable=False)


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


class ControlLiveEvent(Base):
    """Sanitized, replayable event for one task or autonomous monitor stream."""

    __tablename__ = "control_live_events"

    id = Column(Text, primary_key=True, default=_uuid)
    user_id = Column(Text, ForeignKey("users.id"), nullable=False)
    target_key = Column(Text, nullable=False)
    sequence = Column(BigInteger, nullable=False)
    task_id = Column(Text, nullable=True)
    monitor_id = Column(Text, nullable=True)
    worker_task_id = Column(Text, nullable=True)
    event_type = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("target_key", "sequence", name="uq_control_live_event_target_sequence"),
        Index("ix_control_live_event_target_created", "target_key", "created_at"),
    )
