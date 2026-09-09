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


class CapabilityOsMcpOAuthFlow(Base):
    __tablename__ = "capability_os_mcp_oauth_flows"

    flow_id = Column(Text, primary_key=True, default=lambda: _id("mcp_oauth"))
    user_id = Column(Text, nullable=False)
    task_id = Column(Text, nullable=False)
    artifact_digest = Column(Text, nullable=False)
    derived_artifact_digest = Column(Text, nullable=False)
    server_id = Column(Text, nullable=False)
    remote_url = Column(Text, nullable=False)
    profile_id = Column(Text, nullable=False)
    logical_name = Column(Text, nullable=False)
    redirect_uri = Column(Text, nullable=False)
    state_hash = Column(Text, nullable=False)
    code_verifier_encrypted = Column(Text, nullable=False)
    client_info_encrypted = Column(Text, nullable=False)
    protected_resource_metadata = Column(JSON, nullable=True)
    oauth_metadata = Column(JSON, nullable=True)
    scope = Column(Text, nullable=True)
    status = Column(Text, nullable=False)
    created_at_ms = Column(BigInteger, nullable=False)
    expires_at_ms = Column(BigInteger, nullable=False)
    completed_at_ms = Column(BigInteger, nullable=True)
    error_code = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("state_hash", name="uq_capability_os_mcp_oauth_state_hash"),
        Index("ix_capability_os_mcp_oauth_flow_user_status", "user_id", "status"),
        Index("ix_capability_os_mcp_oauth_flow_expiry", "status", "expires_at_ms"),
    )


class CapabilityOsMcpOAuthCredential(Base):
    __tablename__ = "capability_os_mcp_oauth_credentials"

    logical_name = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False)
    profile_id = Column(Text, nullable=False)
    server_id = Column(Text, nullable=False)
    remote_url = Column(Text, nullable=False)
    consumer = Column(Text, nullable=False)
    access_token_encrypted = Column(Text, nullable=False)
    refresh_token_encrypted = Column(Text, nullable=True)
    token_type = Column(Text, nullable=False)
    scope = Column(Text, nullable=True)
    expires_at_ms = Column(BigInteger, nullable=True)
    client_info_encrypted = Column(Text, nullable=False)
    protected_resource_metadata = Column(JSON, nullable=True)
    oauth_metadata = Column(JSON, nullable=True)
    redirect_uri = Column(Text, nullable=False)
    updated_at_ms = Column(BigInteger, nullable=False)
    revoked_at_ms = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "profile_id",
            "server_id",
            "remote_url",
            name="uq_capability_os_mcp_oauth_user_profile_remote",
        ),
        Index("ix_capability_os_mcp_oauth_credential_user", "user_id", "profile_id"),
        Index("ix_capability_os_mcp_oauth_credential_remote", "server_id", "remote_url"),
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


# ---------------------------------------------------------------------------
# 0035 – Experiments, Promotions, Lineage, Policy Decisions, Observations
# ---------------------------------------------------------------------------

class CapabilityOsExperiment(Base):
    __tablename__ = "capability_os_experiments"

    id = Column(Text, primary_key=True, default=lambda: _id("cexp"))
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    artifact_id = Column(Text, nullable=False)
    baseline_version = Column(Text, nullable=False)
    candidate_version = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    traffic_split = Column(sa.Float, nullable=False, default=0.5)
    owner = Column(Text, nullable=False)
    created_at = Column(sa.Float, nullable=False)
    updated_at = Column(sa.Float, nullable=True)
    concluded_at = Column(sa.Float, nullable=True)

    __table_args__ = (
        Index("ix_capability_os_experiment_artifact_status", "artifact_id", "status"),
        Index("ix_capability_os_experiment_owner", "owner", "created_at"),
    )


class CapabilityOsExperimentRun(Base):
    __tablename__ = "capability_os_experiment_runs"

    id = Column(Text, primary_key=True, default=lambda: _id("crun"))
    experiment_id = Column(Text, nullable=False)
    variant = Column(Text, nullable=False)
    task_id = Column(Text, nullable=True)
    status = Column(Text, nullable=False)
    latency_ms = Column(sa.Float, nullable=True)
    success = Column(sa.Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(sa.Float, nullable=False)
    completed_at = Column(sa.Float, nullable=True)

    __table_args__ = (
        Index("ix_capability_os_experiment_run_experiment", "experiment_id", "variant"),
        Index("ix_capability_os_experiment_run_task", "task_id", "created_at"),
    )


class CapabilityOsPromotion(Base):
    __tablename__ = "capability_os_promotions"

    id = Column(Text, primary_key=True, default=lambda: _id("cpro"))
    artifact_id = Column(Text, nullable=False)
    from_version = Column(Text, nullable=False)
    to_version = Column(Text, nullable=False)
    from_stage = Column(Text, nullable=False)
    to_stage = Column(Text, nullable=False)
    experiment_id = Column(Text, nullable=True)
    promoted_by = Column(Text, nullable=False)
    reason = Column(Text, nullable=True)
    policy_decision_id = Column(Text, nullable=True)
    created_at = Column(sa.Float, nullable=False)

    __table_args__ = (
        Index("ix_capability_os_promotion_artifact", "artifact_id", "created_at"),
        Index("ix_capability_os_promotion_stage", "from_stage", "to_stage"),
    )


class CapabilityOsLineageEdge(Base):
    __tablename__ = "capability_os_lineage_edges"

    id = Column(Text, primary_key=True, default=lambda: _id("clin"))
    parent_id = Column(Text, nullable=False)
    child_id = Column(Text, nullable=False)
    edge_type = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(sa.Float, nullable=False)

    __table_args__ = (
        Index("ix_capability_os_lineage_parent", "parent_id", "edge_type"),
        Index("ix_capability_os_lineage_child", "child_id", "edge_type"),
    )


class CapabilityOsPolicyDecision(Base):
    __tablename__ = "capability_os_policy_decisions"

    id = Column(Text, primary_key=True, default=lambda: _id("cpol"))
    policy_id = Column(Text, nullable=False)
    policy_version = Column(Text, nullable=False)
    subject = Column(Text, nullable=False)
    action = Column(Text, nullable=False)
    resource = Column(Text, nullable=True)
    decision = Column(Text, nullable=False)
    reason = Column(Text, nullable=True)
    context_json = Column(Text, nullable=True)
    latency_ms = Column(sa.Float, nullable=True)
    created_at = Column(sa.Float, nullable=False)

    __table_args__ = (
        Index("ix_capability_os_policy_decision_subject", "subject", "decision"),
        Index("ix_capability_os_policy_decision_policy", "policy_id", "created_at"),
    )


class CapabilityOsObservation(Base):
    __tablename__ = "capability_os_observations"

    id = Column(Text, primary_key=True, default=lambda: _id("cobs"))
    artifact_id = Column(Text, nullable=False)
    artifact_version = Column(Text, nullable=False)
    task_id = Column(Text, nullable=True)
    metric_name = Column(Text, nullable=False)
    metric_value = Column(sa.Float, nullable=False)
    unit = Column(Text, nullable=True)
    tags_json = Column(Text, nullable=True)
    source = Column(Text, nullable=True)
    count = Column(sa.Integer, nullable=False, default=1)
    created_at = Column(sa.Float, nullable=False)

    __table_args__ = (
        Index("ix_capability_os_observation_artifact", "artifact_id", "metric_name"),
        Index("ix_capability_os_observation_task", "task_id", "created_at"),
    )
