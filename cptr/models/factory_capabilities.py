"""Durable normalized capability manifests and verified performance memory."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class FactoryCapabilityRecord(Base):
    __tablename__ = "factory_capabilities"

    id = Column(Text, primary_key=True)
    stable_id = Column(Text, nullable=False)
    version = Column(Text, nullable=False)
    origin_type = Column(Text, nullable=False)
    origin_uri = Column(Text, nullable=False)
    pinned_version_or_commit = Column(Text, nullable=True)
    digest = Column(Text, nullable=False)
    capabilities = Column(JSON, nullable=False, default=list)
    permissions = Column(JSON, nullable=False, default=list)
    network_requirements = Column(JSON, nullable=False, default=list)
    execution_requirements = Column(JSON, nullable=False, default=list)
    risk_classification = Column(Text, nullable=False)
    trust_status = Column(Text, nullable=False)
    verification_status = Column(Text, nullable=False)
    maintenance_metadata = Column(JSON, nullable=False, default=dict)
    historical_factory_score_ppm = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    evaluated_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "stable_id",
            "version",
            "digest",
            name="uq_factory_capability_immutable_identity",
        ),
        Index("ix_factory_capability_stable_version", "stable_id", "version"),
        Index("ix_factory_capability_trust", "trust_status", "verification_status"),
    )


class FactoryCapabilityPerformance(Base):
    __tablename__ = "factory_capability_performance"

    id = Column(Text, primary_key=True, default=lambda: _id("fcapperf"))
    capability_id = Column(
        Text,
        ForeignKey("factory_capabilities.id", ondelete="CASCADE"),
        nullable=False,
    )
    repository_family = Column(Text, nullable=False)
    task_family = Column(Text, nullable=False)
    attempts = Column(BigInteger, nullable=False, default=0)
    verified_successes = Column(BigInteger, nullable=False, default=0)
    verified_failures = Column(BigInteger, nullable=False, default=0)
    regressions = Column(BigInteger, nullable=False, default=0)
    repair_iterations = Column(BigInteger, nullable=False, default=0)
    input_tokens = Column(BigInteger, nullable=False, default=0)
    output_tokens = Column(BigInteger, nullable=False, default=0)
    runtime_ms = Column(BigInteger, nullable=False, default=0)
    cost_microusd = Column(BigInteger, nullable=False, default=0)
    confidence_ppm = Column(BigInteger, nullable=False, default=0)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "capability_id",
            "repository_family",
            "task_family",
            name="uq_factory_capability_performance_family",
        ),
        Index(
            "ix_factory_capability_performance_lookup",
            "capability_id",
            "repository_family",
            "task_family",
        ),
    )
