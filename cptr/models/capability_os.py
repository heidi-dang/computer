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


class CapabilityOsRun(Base):
    __tablename__ = "capability_os_runs"

    run_id = Column(Text, primary_key=True, default=lambda: _id("crun"))
    task_id = Column(Text, nullable=False)
    capability_digest = Column(Text, nullable=False)
    lease_id = Column(Text, ForeignKey("capability_os_leases.lease_id", ondelete="SET NULL"), nullable=True)
    status = Column(Text, nullable=False)
    inputs_digest = Column(Text, nullable=True)
    started_at_ms = Column(BigInteger, nullable=False)
    completed_at_ms = Column(BigInteger, nullable=True)
    error_code = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_capability_os_run_task_started", "task_id", "started_at_ms"),
        Index("ix_capability_os_run_artifact_status", "capability_digest", "status"),
    )


class CapabilityOsRunStep(Base):
    __tablename__ = "capability_os_run_steps"

    step_id = Column(Text, primary_key=True, default=lambda: _id("cstep"))
    run_id = Column(Text, ForeignKey("capability_os_runs.run_id", ondelete="CASCADE"), nullable=False)
    node_id = Column(Text, nullable=False)
    attempt = Column(BigInteger, nullable=False, default=1)
    status = Column(Text, nullable=False)
    output_digest = Column(Text, nullable=True)
    error_code = Column(Text, nullable=True)
    started_at_ms = Column(BigInteger, nullable=False)
    completed_at_ms = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "node_id", "attempt", name="uq_capability_os_run_step_attempt"),
        Index("ix_capability_os_run_step_run_status", "run_id", "status"),
    )


class CapabilityOsExperiment(Base):
    __tablename__ = "capability_os_experiments"

    experiment_id = Column(Text, primary_key=True)
    task_id = Column(Text, nullable=False)
    change_class = Column(Text, nullable=False)
    mode = Column(Text, nullable=False)
    hypothesis = Column(Text, nullable=False)
    control_artifact_digest = Column(Text, nullable=False)
    candidate_artifact_digest = Column(Text, nullable=False)
    context = Column(JSON, nullable=False, default=dict)
    min_runs_per_arm = Column(BigInteger, nullable=False)
    state = Column(Text, nullable=False)
    created_at_ms = Column(BigInteger, nullable=False)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_capability_os_experiment_task_state", "task_id", "state"),
        Index("ix_capability_os_experiment_candidate", "candidate_artifact_digest", "created_at_ms"),
    )


class CapabilityOsExperimentRun(Base):
    __tablename__ = "capability_os_experiment_runs"

    experiment_run_id = Column(Text, primary_key=True, default=lambda: _id("exprun"))
    experiment_id = Column(Text, ForeignKey("capability_os_experiments.experiment_id", ondelete="CASCADE"), nullable=False)
    arm = Column(Text, nullable=False)
    source_evidence_id = Column(Text, nullable=False)
    source_evidence_digest = Column(Text, nullable=False)
    success = Column(BigInteger, nullable=False)
    regression = Column(BigInteger, nullable=False)
    safety_events = Column(BigInteger, nullable=False)
    cost = Column(Text, nullable=False)
    created_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("experiment_id", "source_evidence_id", name="uq_capability_os_experiment_source"),
        Index("ix_capability_os_experiment_run_arm", "experiment_id", "arm", "created_at_ms"),
    )


class CapabilityOsPromotion(Base):
    __tablename__ = "capability_os_promotions"

    promotion_id = Column(Text, primary_key=True, default=lambda: _id("promotion"))
    experiment_id = Column(Text, ForeignKey("capability_os_experiments.experiment_id", ondelete="RESTRICT"), nullable=False)
    candidate_artifact_digest = Column(Text, nullable=False)
    decision = Column(Text, nullable=False)
    from_state = Column(Text, nullable=False)
    target_state = Column(Text, nullable=False)
    evaluation_evidence_id = Column(Text, nullable=False)
    intent_evidence_id = Column(Text, nullable=False)
    promotion_evidence_id = Column(Text, nullable=False)
    owner_approval_verified = Column(BigInteger, nullable=False)
    created_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("promotion_evidence_id", name="uq_capability_os_promotion_evidence"),
        Index("ix_capability_os_promotion_experiment", "experiment_id", "created_at_ms"),
    )


class CapabilityOsLineageEdge(Base):
    __tablename__ = "capability_os_lineage_edges"

    edge_id = Column(Text, primary_key=True, default=lambda: _id("lineage"))
    child_digest = Column(Text, nullable=False)
    parent_ref = Column(Text, nullable=False)
    relation = Column(Text, nullable=False)
    operator = Column(Text, nullable=True)
    created_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("child_digest", "parent_ref", "relation", name="uq_capability_os_lineage_edge"),
        Index("ix_capability_os_lineage_child", "child_digest", "created_at_ms"),
    )


class CapabilityOsPolicyDecision(Base):
    __tablename__ = "capability_os_policy_decisions"

    decision_id = Column(Text, primary_key=True)
    task_id = Column(Text, nullable=False)
    workload_id = Column(Text, nullable=False)
    artifact_digest = Column(Text, nullable=False)
    outcome = Column(Text, nullable=False)
    permissions = Column(JSON, nullable=False, default=list)
    constraints = Column(JSON, nullable=False, default=dict)
    approval_id = Column(Text, nullable=True)
    reason_code = Column(Text, nullable=True)
    created_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_capability_os_policy_task_created", "task_id", "created_at_ms"),
        Index("ix_capability_os_policy_artifact", "artifact_digest", "created_at_ms"),
    )


class CapabilityOsObservation(Base):
    __tablename__ = "capability_os_observations"

    observation_id = Column(Text, primary_key=True, default=lambda: _id("observation"))
    task_id = Column(Text, nullable=False)
    run_id = Column(Text, nullable=True)
    kind = Column(Text, nullable=False)
    artifact_digest = Column(Text, nullable=True)
    evidence_id = Column(Text, nullable=True)
    metrics = Column(JSON, nullable=False, default=dict)
    created_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("evidence_id", name="uq_capability_os_observation_evidence"),
        Index("ix_capability_os_observation_task", "task_id", "created_at_ms"),
        Index("ix_capability_os_observation_run", "run_id", "created_at_ms"),
    )


class CapabilityOsRetentionJob(Base):
    __tablename__ = "capability_os_retention_jobs"

    retention_job_id = Column(Text, primary_key=True, default=lambda: _id("retention"))
    task_id = Column(Text, nullable=True)
    artifact_digest = Column(Text, nullable=True)
    policy = Column(JSON, nullable=False, default=dict)
    status = Column(Text, nullable=False)
    scheduled_at_ms = Column(BigInteger, nullable=False)
    started_at_ms = Column(BigInteger, nullable=True)
    completed_at_ms = Column(BigInteger, nullable=True)
    error_code = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_capability_os_retention_status_schedule", "status", "scheduled_at_ms"),
        Index("ix_capability_os_retention_artifact", "artifact_digest", "scheduled_at_ms"),
    )


class CapabilityOsMcpDiscoveryEntry(Base):
    __tablename__ = "capability_os_mcp_discovery_entries"

    entry_id = Column(Text, primary_key=True)
    provider = Column(Text, nullable=False)
    server_id = Column(Text, nullable=False)
    version = Column(Text, nullable=True)
    origin_uri = Column(Text, nullable=False)
    source_uri = Column(Text, nullable=True)
    effects = Column(JSON, nullable=False, default=list)
    permissions = Column(JSON, nullable=False, default=list)
    metadata_json = Column("metadata", JSON, nullable=False, default=dict)
    refreshed_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_capability_os_mcp_discovery_provider", "provider", "refreshed_at_ms"),
        Index("ix_capability_os_mcp_discovery_server", "server_id", "version"),
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
