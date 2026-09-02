"""Durable Dark Factory run, cycle, event, evidence, and gate models."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class FactoryRun(Base):
    __tablename__ = "factory_runs"

    id = Column(Text, primary_key=True, default=lambda: _id("factory"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    mission = Column(Text, nullable=False)
    acceptance_criteria = Column(JSON, nullable=False, default=list)
    model_id = Column(Text, nullable=True)
    state = Column(Text, nullable=False)
    current_cycle_id = Column(Text, nullable=True)
    resumable_state = Column(Text, nullable=True)
    policy = Column(JSON, nullable=False, default=dict)
    budget = Column(JSON, nullable=False, default=dict)
    config_fingerprint = Column(Text, nullable=False)
    next_action = Column(Text, nullable=True)
    idempotency_key = Column(Text, nullable=True)
    lease_token = Column(Text, nullable=True)
    lease_expires_at = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    completed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_factory_run_user_idempotency"),
        Index("ix_factory_run_user_state_updated", "user_id", "state", "updated_at"),
        Index("ix_factory_run_workspace_state", "workspace_id", "state"),
    )


class FactoryCycle(Base):
    __tablename__ = "factory_cycles"

    id = Column(Text, primary_key=True, default=lambda: _id("cycle"))
    run_id = Column(Text, ForeignKey("factory_runs.id", ondelete="CASCADE"), nullable=False)
    ordinal = Column(BigInteger, nullable=False)
    state = Column(Text, nullable=False)
    idempotency_key = Column(Text, nullable=True)
    selected_finding = Column(JSON, nullable=True)
    capability_requirements = Column(JSON, nullable=False, default=list)
    selected_capabilities = Column(JSON, nullable=False, default=list)
    base_revision = Column(Text, nullable=True)
    base_fingerprint = Column(Text, nullable=True)
    target_revision = Column(Text, nullable=True)
    target_fingerprint = Column(Text, nullable=True)
    mutation_worker_id = Column(Text, nullable=True)
    attempt_count = Column(BigInteger, nullable=False, default=0)
    failure_signatures = Column(JSON, nullable=False, default=dict)
    next_action = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    completed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_factory_cycle_run_ordinal"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_factory_cycle_run_idempotency"),
        Index("ix_factory_cycle_run_state", "run_id", "state"),
    )


class FactoryEvent(Base):
    __tablename__ = "factory_events"

    id = Column(Text, primary_key=True, default=lambda: _id("fev"))
    run_id = Column(Text, ForeignKey("factory_runs.id", ondelete="CASCADE"), nullable=False)
    cycle_id = Column(Text, ForeignKey("factory_cycles.id", ondelete="SET NULL"), nullable=True)
    sequence = Column(BigInteger, nullable=False)
    actor = Column(Text, nullable=False)
    event_type = Column(Text, nullable=False)
    from_state = Column(Text, nullable=True)
    to_state = Column(Text, nullable=True)
    idempotency_key = Column(Text, nullable=True)
    payload_digest = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_factory_event_run_sequence"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_factory_event_run_idempotency"),
        Index("ix_factory_event_run_created", "run_id", "created_at"),
    )


class FactoryEvidence(Base):
    __tablename__ = "factory_evidence"

    id = Column(Text, primary_key=True, default=lambda: _id("fevidence"))
    run_id = Column(Text, ForeignKey("factory_runs.id", ondelete="CASCADE"), nullable=False)
    cycle_id = Column(Text, ForeignKey("factory_cycles.id", ondelete="SET NULL"), nullable=True)
    gate_id = Column(Text, nullable=True)
    kind = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    authority = Column(Text, nullable=False)
    revision = Column(Text, nullable=True)
    fingerprint = Column(Text, nullable=True)
    digest = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_factory_evidence_run_created", "run_id", "created_at"),
        Index("ix_factory_evidence_cycle_gate", "cycle_id", "gate_id"),
    )


class FactoryReasoningCall(Base):
    __tablename__ = "factory_reasoning_calls"

    id = Column(Text, primary_key=True, default=lambda: _id("freason"))
    run_id = Column(Text, ForeignKey("factory_runs.id", ondelete="CASCADE"), nullable=False)
    cycle_id = Column(Text, ForeignKey("factory_cycles.id", ondelete="CASCADE"), nullable=False)
    role = Column(Text, nullable=False)
    role_ordinal = Column(BigInteger, nullable=False)
    schema_id = Column(Text, nullable=False)
    provider = Column(Text, nullable=False)
    model = Column(Text, nullable=False)
    response_id = Column(Text, nullable=True)
    input_tokens = Column(BigInteger, nullable=False, default=0)
    output_tokens = Column(BigInteger, nullable=False, default=0)
    total_tokens = Column(BigInteger, nullable=False, default=0)
    runtime_ms = Column(BigInteger, nullable=False, default=0)
    cost_microusd = Column(BigInteger, nullable=False, default=0)
    attempt_count = Column(BigInteger, nullable=False, default=1)
    data = Column(JSON, nullable=False, default=dict)
    provider_metadata = Column(JSON, nullable=False, default=dict)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "role",
            "role_ordinal",
            name="uq_factory_reasoning_cycle_role_ordinal",
        ),
        Index(
            "ix_factory_reasoning_run_cycle_role",
            "run_id",
            "cycle_id",
            "role",
            "role_ordinal",
        ),
    )


class FactoryGateResult(Base):
    __tablename__ = "factory_gate_results"

    id = Column(Text, primary_key=True, default=lambda: _id("fgate"))
    run_id = Column(Text, ForeignKey("factory_runs.id", ondelete="CASCADE"), nullable=False)
    cycle_id = Column(Text, ForeignKey("factory_cycles.id", ondelete="CASCADE"), nullable=False)
    gate_id = Column(Text, nullable=False)
    category = Column(Text, nullable=False)
    required = Column(Boolean, nullable=False, default=True)
    applicable = Column(Boolean, nullable=False, default=True)
    status = Column(Text, nullable=False)
    evidence_ids = Column(JSON, nullable=False, default=list)
    evaluated_revision = Column(Text, nullable=True)
    evaluated_fingerprint = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    attempt = Column(BigInteger, nullable=False, default=1)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "gate_id",
            "attempt",
            name="uq_factory_gate_cycle_gate_attempt",
        ),
        Index("ix_factory_gate_run_cycle", "run_id", "cycle_id"),
        Index("ix_factory_gate_cycle_gate", "cycle_id", "gate_id"),
    )
