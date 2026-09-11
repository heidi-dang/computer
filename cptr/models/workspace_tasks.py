"""Database models for WorkspaceTask aggregation over Direct Coding workers."""

from __future__ import annotations

import uuid
from sqlalchemy import BigInteger, Column, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


def _task_id() -> str:
    return f"wst_{uuid.uuid4().hex}"


def _pin_id() -> str:
    return f"wspin_{uuid.uuid4().hex}"


def _link_id() -> str:
    return f"wtl_{uuid.uuid4().hex}"


def _evidence_id() -> str:
    return f"wste_{uuid.uuid4().hex}"


class WorkspaceTask(Base):
    """Aggregated engineering task across repositories and Direct Coding workers."""

    __tablename__ = "workspace_tasks"

    id = Column(Text, primary_key=True, default=_task_id)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=False, default="")
    status = Column(
        Text, nullable=False, default="OPEN"
    )  # OPEN, IN_PROGRESS, VERIFIED, INTEGRATED, CLOSED, FAILED
    task_metadata = Column("metadata", JSON, nullable=False, default=dict)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    closed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_workspace_tasks_user_workspace_status", "user_id", "workspace_id", "status"),
        Index("ix_workspace_tasks_user_updated", "user_id", "updated_at"),
    )


class WorkspaceTaskRepositoryPin(Base):
    """Pinned repository revision baseline for a workspace task."""

    __tablename__ = "workspace_task_repository_pins"

    id = Column(Text, primary_key=True, default=_pin_id)
    task_id = Column(Text, ForeignKey("workspace_tasks.id", ondelete="CASCADE"), nullable=False)
    repo_path = Column(Text, nullable=False, default=".")
    pinned_revision = Column(Text, nullable=False)
    head_revision = Column(
        Text, nullable=True
    )  # current HEAD at last pin/update; NULL until first capture
    branch = Column(Text, nullable=True)
    pinned_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("task_id", "repo_path", name="uq_workspace_task_repo_pin"),
        Index("ix_workspace_task_pins_task_id", "task_id"),
    )


class WorkspaceTaskWorkerLink(Base):
    """Association between a WorkspaceTask and a DirectCodingWorker."""

    __tablename__ = "workspace_task_worker_links"

    id = Column(Text, primary_key=True, default=_link_id)
    task_id = Column(Text, ForeignKey("workspace_tasks.id", ondelete="CASCADE"), nullable=False)
    worker_id = Column(
        Text, ForeignKey("direct_coding_workers.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(Text, nullable=False, default="contributor")
    status = Column(Text, nullable=False, default="ACTIVE")  # ACTIVE, DETACHED, INTEGRATED
    linked_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("task_id", "worker_id", name="uq_workspace_task_worker_link"),
        Index("ix_workspace_task_worker_links_task_id", "task_id"),
        Index("ix_workspace_task_worker_links_worker_id", "worker_id"),
    )


class WorkspaceTaskEvidence(Base):
    """Verification and validation evidence attached to a workspace task."""

    __tablename__ = "workspace_task_evidence"

    id = Column(Text, primary_key=True, default=_evidence_id)
    task_id = Column(Text, ForeignKey("workspace_tasks.id", ondelete="CASCADE"), nullable=False)
    worker_id = Column(Text, nullable=True)
    repo_path = Column(Text, nullable=True)
    kind = Column(Text, nullable=False)  # test, lint, typecheck, diff, review, command, manual
    status = Column(Text, nullable=False)  # PASSED, FAILED, PENDING, OBSERVED
    summary = Column(Text, nullable=False)
    command = Column(Text, nullable=True)
    details = Column(JSON, nullable=False, default=dict)
    fingerprint = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_workspace_task_evidence_task_kind", "task_id", "kind"),
        Index("ix_workspace_task_evidence_task_status", "task_id", "status"),
        Index("ix_workspace_task_evidence_task_created", "task_id", "created_at"),
    )
