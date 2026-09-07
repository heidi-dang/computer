"""Durable Capability OS artefacts, authority leases, evidence, and MCP mounts."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Column, ForeignKey, Index, JSON, Text, UniqueConstraint

from cptr.models.base import Base


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class CapabilityOsArtifact(Base):
    __tablename__ = "capability_os_artifacts"

    id = Column(Text, primary_key=True, default=lambda: _id("cart"))
    artifact_id = Column(Text, nullable=False)
    version = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)
    owner = Column(Text, nullable=False)
    user_id = Column(Text, nullable=True)
    origin = Column(Text, nullable=False)
    state = Column(Text, nullable=False)
    parent_ref = Column(Text, nullable=True)
    task_origin = Column(Text, nullable=True)
    source_digest = Column(Text, nullable=True)
    content_digest = Column(Text, nullable=False)
    compatibility = Column(JSON, nullable=False, default=dict)
    spec = Column(JSON, nullable=False, default=dict)
    created_at = Column(Text, nullable=False)
    created_at_ms = Column(BigInteger, nullable=False)
    retired_at_ms = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "version",
            "content_digest",
            name="uq_capability_os_artifact_immutable_identity",
        ),
        UniqueConstraint("content_digest", name="uq_capability_os_artifact_content_digest"),
        Index("ix_capability_os_artifact_kind_state", "kind", "state"),
        Index("ix_capability_os_artifact_user_state", "user_id", "state"),
        Index("ix_capability_os_artifact_identity", "artifact_id", "version"),
        Index("ix_capability_os_artifact_task", "task_origin", "created_at_ms"),
    )


class CapabilityOsLease(Base):
    __tablename__ = "capability_os_leases"

    lease_id = Column(Text, primary_key=True, default=lambda: _id("lease"))
    task_id = Column(Text, nullable=False)
    workload_id = Column(Text, nullable=False)
    artifact_digest = Column(Text, nullable=False)
    permissions = Column(JSON, nullable=False, default=list)
    resource_limits = Column(JSON, nullable=False, default=dict)
    network = Column(JSON, nullable=False, default=dict)
    credentials = Column(JSON, nullable=False, default=dict)
    approval_id = Column(Text, nullable=True)
    parent_lease_id = Column(
        Text,
        ForeignKey("capability_os_leases.lease_id", ondelete="SET NULL"),
        nullable=True,
    )
    policy_decision_id = Column(Text, nullable=False)
    attestation_requirements = Column(JSON, nullable=False, default=dict)
    runtime_profile = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    issued_at_ms = Column(BigInteger, nullable=False)
    expires_at_ms = Column(BigInteger, nullable=False)
    revoked_at_ms = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_capability_os_lease_task_status", "task_id", "status"),
        Index("ix_capability_os_lease_artifact", "artifact_digest", "status"),
        Index("ix_capability_os_lease_expiry", "status", "expires_at_ms"),
    )


class CapabilityOsEvidence(Base):
    __tablename__ = "capability_os_evidence"

    evidence_id = Column(Text, primary_key=True, default=lambda: _id("cevidence"))
    task_id = Column(Text, nullable=False)
    run_id = Column(Text, nullable=True)
    kind = Column(Text, nullable=False)
    producer_identity = Column(Text, nullable=False)
    claims = Column(JSON, nullable=False, default=dict)
    blob_uri = Column(Text, nullable=True)
    sequence = Column(BigInteger, nullable=False)
    previous_digest = Column(Text, nullable=True)
    digest = Column(Text, nullable=False)
    artifact_digest = Column(Text, nullable=True)
    lease_id = Column(
        Text,
        ForeignKey("capability_os_leases.lease_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "sequence",
            name="uq_capability_os_evidence_task_sequence",
        ),
        Index("ix_capability_os_evidence_task_created", "task_id", "created_at_ms"),
        Index("ix_capability_os_evidence_artifact", "artifact_digest", "created_at_ms"),
        Index("ix_capability_os_evidence_lease", "lease_id", "created_at_ms"),
    )


class CapabilityOsMcpMount(Base):
    __tablename__ = "capability_os_mcp_mounts"

    mount_id = Column(Text, primary_key=True, default=lambda: _id("mcp_mount"))
    task_id = Column(Text, nullable=False)
    server_id = Column(Text, nullable=False)
    version = Column(Text, nullable=False)
    digest = Column(Text, nullable=False)
    state = Column(Text, nullable=False)
    lease_id = Column(
        Text,
        ForeignKey("capability_os_leases.lease_id", ondelete="RESTRICT"),
        nullable=False,
    )
    projected_tools = Column(JSON, nullable=False, default=list)
    transport_kind = Column(Text, nullable=False)
    created_at_ms = Column(BigInteger, nullable=False)
    updated_at_ms = Column(BigInteger, nullable=False)
    released_at_ms = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_capability_os_mcp_mount_task_state", "task_id", "state"),
        Index("ix_capability_os_mcp_mount_server", "server_id", "version", "digest"),
    )
