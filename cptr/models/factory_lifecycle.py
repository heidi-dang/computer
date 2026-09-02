"""Durable Git and CI lifecycle projections for Dark Factory cycles."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class FactoryCommitIntent(Base):
    __tablename__ = "factory_commit_intents"

    id = Column(Text, primary_key=True, default=lambda: _id("fcommit"))
    run_id = Column(Text, ForeignKey("factory_runs.id", ondelete="CASCADE"), nullable=False)
    cycle_id = Column(Text, ForeignKey("factory_cycles.id", ondelete="CASCADE"), nullable=False)
    repository_key = Column(Text, nullable=False)
    verified_revision = Column(Text, nullable=False)
    verified_fingerprint = Column(Text, nullable=False)
    diff_digest = Column(Text, nullable=False)
    changed_paths = Column(JSON, nullable=False, default=list)
    commit_message = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="PREPARED")
    commit_sha = Column(Text, nullable=True)
    push_status = Column(Text, nullable=True)
    push_remote = Column(Text, nullable=True)
    push_branch = Column(Text, nullable=True)
    push_approval_id = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    committed_at = Column(BigInteger, nullable=True)
    pushed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("cycle_id", name="uq_factory_commit_intent_cycle"),
        Index("ix_factory_commit_intent_run_status", "run_id", "status"),
        Index("ix_factory_commit_intent_commit_sha", "commit_sha"),
    )


class FactoryCiRun(Base):
    __tablename__ = "factory_ci_runs"

    id = Column(Text, primary_key=True, default=lambda: _id("fci"))
    run_id = Column(Text, ForeignKey("factory_runs.id", ondelete="CASCADE"), nullable=False)
    cycle_id = Column(Text, ForeignKey("factory_cycles.id", ondelete="CASCADE"), nullable=False)
    provider = Column(Text, nullable=False)
    repository = Column(Text, nullable=False)
    revision = Column(Text, nullable=False)
    external_run_id = Column(Text, nullable=False)
    check_id = Column(Text, nullable=False, default="")
    status = Column(Text, nullable=False, default="QUEUED")
    conclusion = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    failure_summary = Column(Text, nullable=True)
    diagnosis_required = Column(Boolean, nullable=False, default=False)
    diagnosis_summary = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    last_observed_at = Column(BigInteger, nullable=True)
    diagnosed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "repository",
            "external_run_id",
            "check_id",
            name="uq_factory_ci_provider_run_check",
        ),
        Index("ix_factory_ci_cycle_revision", "cycle_id", "revision"),
        Index("ix_factory_ci_run_status", "run_id", "status", "conclusion"),
    )
