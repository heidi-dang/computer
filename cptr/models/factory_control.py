"""Durable user-control projections for the Dark Factory API surface."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Text, UniqueConstraint

from cptr.models.base import Base


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class FactoryApproval(Base):
    __tablename__ = "factory_approvals"

    id = Column(Text, primary_key=True, default=lambda: _id("fapproval"))
    run_id = Column(Text, ForeignKey("factory_runs.id", ondelete="CASCADE"), nullable=False)
    cycle_id = Column(Text, ForeignKey("factory_cycles.id", ondelete="CASCADE"), nullable=False)
    kind = Column(Text, nullable=False)
    operation_digest = Column(Text, nullable=False)
    revision = Column(Text, nullable=False)
    remote = Column(Text, nullable=False)
    branch = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="PENDING")
    decision_idempotency_key = Column(Text, nullable=True)
    decision_digest = Column(Text, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    decided_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "kind",
            "operation_digest",
            name="uq_factory_approval_cycle_operation",
        ),
        UniqueConstraint(
            "run_id",
            "decision_idempotency_key",
            name="uq_factory_approval_run_decision_idempotency",
        ),
        Index("ix_factory_approval_run_status", "run_id", "status", "updated_at"),
        Index("ix_factory_approval_cycle_kind", "cycle_id", "kind"),
    )
