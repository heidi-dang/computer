"""Durable numeric-only Dark Factory metric and capability-outcome projections."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Index, Text, UniqueConstraint

from cptr.models.base import Base


class FactoryMetricProjection(Base):
    """Latest numeric projection for one run/cycle metric dimension.

    This table intentionally contains no prompt, mission, source, model reasoning,
    gate reason, provider prose, or arbitrary JSON payload columns.
    """

    __tablename__ = "factory_metric_projections"

    id = Column(Text, primary_key=True)
    run_id = Column(Text, ForeignKey("factory_runs.id", ondelete="CASCADE"), nullable=False)
    cycle_id = Column(Text, ForeignKey("factory_cycles.id", ondelete="CASCADE"), nullable=True)
    scope = Column(Text, nullable=False)
    dimension_key = Column(Text, nullable=False, default="")
    attempts = Column(BigInteger, nullable=False, default=0)
    repair_iterations = Column(BigInteger, nullable=False, default=0)
    regressions = Column(BigInteger, nullable=False, default=0)
    input_tokens = Column(BigInteger, nullable=False, default=0)
    output_tokens = Column(BigInteger, nullable=False, default=0)
    runtime_ms = Column(BigInteger, nullable=False, default=0)
    cost_microusd = Column(BigInteger, nullable=False, default=0)
    gate_latency_ms = Column(BigInteger, nullable=False, default=0)
    verified_outcome = Column(Text, nullable=True)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_factory_metric_run_scope", "run_id", "scope", "dimension_key"),
        Index("ix_factory_metric_cycle_scope", "cycle_id", "scope", "dimension_key"),
    )


class FactoryCapabilityOutcome(Base):
    """Idempotent proof-bound learning event for one capability in one cycle."""

    __tablename__ = "factory_capability_outcomes"

    id = Column(Text, primary_key=True)
    run_id = Column(Text, ForeignKey("factory_runs.id", ondelete="CASCADE"), nullable=False)
    cycle_id = Column(Text, ForeignKey("factory_cycles.id", ondelete="CASCADE"), nullable=False)
    capability_id = Column(
        Text,
        ForeignKey("factory_capabilities.id", ondelete="CASCADE"),
        nullable=False,
    )
    repository_family = Column(Text, nullable=False)
    task_family = Column(Text, nullable=False)
    verified_success = Column(Boolean, nullable=False)
    proof_event_id = Column(
        Text, ForeignKey("factory_events.id", ondelete="RESTRICT"), nullable=False
    )
    regression = Column(Boolean, nullable=False, default=False)
    repair_iterations = Column(BigInteger, nullable=False, default=0)
    input_tokens = Column(BigInteger, nullable=False, default=0)
    output_tokens = Column(BigInteger, nullable=False, default=0)
    runtime_ms = Column(BigInteger, nullable=False, default=0)
    cost_microusd = Column(BigInteger, nullable=False, default=0)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "cycle_id",
            "capability_id",
            name="uq_factory_capability_outcome_run_cycle_capability",
        ),
        Index(
            "ix_factory_capability_outcome_family",
            "capability_id",
            "repository_family",
            "task_family",
        ),
        Index("ix_factory_capability_outcome_run", "run_id", "cycle_id"),
    )
