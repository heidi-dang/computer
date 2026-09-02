"""Durable worker ownership projections for Dark Factory execution."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


def _assignment_id() -> str:
    return f"fworker_{uuid.uuid4().hex}"


class FactoryWorkerAssignment(Base):
    """Durable ownership of one read-only investigation or mutation lane."""

    __tablename__ = "factory_worker_assignments"

    id = Column(Text, primary_key=True, default=_assignment_id)
    run_id = Column(
        Text,
        ForeignKey("factory_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    cycle_id = Column(
        Text,
        ForeignKey("factory_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id = Column(Text, nullable=False)
    worker_id = Column(Text, nullable=True)
    owner_key = Column(Text, nullable=False)
    mode = Column(Text, nullable=False)
    repo_path = Column(Text, nullable=False, default=".")
    scope = Column(JSON, nullable=False, default=list)
    branch = Column(Text, nullable=True)
    base_revision = Column(Text, nullable=True)
    status = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    closed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("worker_id", name="uq_factory_worker_assignment_worker"),
        Index(
            "ix_factory_worker_assignment_run_cycle_status",
            "run_id",
            "cycle_id",
            "status",
        ),
        Index(
            "ix_factory_worker_assignment_workspace_mode_status",
            "workspace_id",
            "mode",
            "status",
        ),
    )
