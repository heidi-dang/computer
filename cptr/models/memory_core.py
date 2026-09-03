"""Canonical and derived persistence for the embedded CPTR Memory Core."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Index, JSON, Text, UniqueConstraint

from cptr.models.base import Base


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class MemoryNamespaceState(Base):
    __tablename__ = "memory_namespace_state"

    id = Column(Text, primary_key=True, default=lambda: _id("memns"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    version = Column(BigInteger, nullable=False, default=0)
    active_snapshot_id = Column(Text, nullable=True)
    active_branch_id = Column(Text, nullable=True)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "workspace", name="uq_memory_namespace_user_workspace"),
        Index("ix_memory_namespace_user_workspace", "user_id", "workspace"),
    )


class MemoryRecord(Base):
    __tablename__ = "memory_records"

    id = Column(Text, primary_key=True, default=lambda: _id("mem"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    scope = Column(Text, nullable=False)
    kind = Column(Text, nullable=False, default="semantic")
    canonical_text = Column(Text, nullable=False)
    structured_value = Column(JSON, nullable=False, default=dict)
    content_hash = Column(Text, nullable=False)
    confidence_ppm = Column(BigInteger, nullable=False, default=850_000)
    importance_ppm = Column(BigInteger, nullable=False, default=500_000)
    trust_level = Column(Text, nullable=False, default="agent_observation")
    status = Column(Text, nullable=False, default="active")
    valid_from_ms = Column(BigInteger, nullable=True)
    valid_until_ms = Column(BigInteger, nullable=True)
    observed_at_ms = Column(BigInteger, nullable=False, default=0)
    superseded_at_ms = Column(BigInteger, nullable=True)
    superseded_by_id = Column(
        Text, ForeignKey("memory_records.id", ondelete="SET NULL"), nullable=True
    )
    source_event_ids = Column(JSON, nullable=False, default=list)
    branch_id = Column(Text, nullable=True)
    parent_memory_id = Column(
        Text, ForeignKey("memory_records.id", ondelete="SET NULL"), nullable=True
    )
    verified_at_ms = Column(BigInteger, nullable=True)
    verification_expires_at_ms = Column(BigInteger, nullable=True)
    access_count = Column(BigInteger, nullable=False, default=0)
    last_accessed_at_ms = Column(BigInteger, nullable=True)
    created_at_ms = Column(BigInteger, nullable=False)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_memory_record_user_workspace_status", "user_id", "workspace", "status"),
        Index("ix_memory_record_user_hash", "user_id", "workspace", "content_hash"),
        Index("ix_memory_record_user_kind", "user_id", "workspace", "kind", "status"),
        Index("ix_memory_record_user_branch", "user_id", "workspace", "branch_id", "status"),
    )


class MemoryCheckpoint(Base):
    __tablename__ = "memory_checkpoints"

    id = Column(Text, primary_key=True, default=lambda: _id("memcp"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    task_key = Column(Text, nullable=False)
    version = Column(BigInteger, nullable=False)
    stage = Column(Text, nullable=False)
    state = Column(JSON, nullable=False, default=dict)
    memory_version = Column(BigInteger, nullable=False, default=0)
    parent_checkpoint_id = Column(
        Text, ForeignKey("memory_checkpoints.id", ondelete="SET NULL"), nullable=True
    )
    created_at_ms = Column(BigInteger, nullable=False)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "workspace", "task_key", "version", name="uq_memory_checkpoint_task_version"
        ),
        Index("ix_memory_checkpoint_task", "user_id", "workspace", "task_key", "version"),
    )


class MemoryEntity(Base):
    __tablename__ = "memory_entities"

    id = Column(Text, primary_key=True, default=lambda: _id("ment"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    canonical_name = Column(Text, nullable=False)
    normalized_name = Column(Text, nullable=False)
    entity_type = Column(Text, nullable=False, default="concept")
    aliases = Column(JSON, nullable=False, default=list)
    source_memory_ids = Column(JSON, nullable=False, default=list)
    status = Column(Text, nullable=False, default="active")
    valid_from_ms = Column(BigInteger, nullable=True)
    valid_until_ms = Column(BigInteger, nullable=True)
    created_at_ms = Column(BigInteger, nullable=False)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "workspace",
            "normalized_name",
            "entity_type",
            name="uq_memory_entity_name_type",
        ),
        Index("ix_memory_entity_user_workspace", "user_id", "workspace", "status"),
    )


class MemoryRelationship(Base):
    __tablename__ = "memory_relationships"

    id = Column(Text, primary_key=True, default=lambda: _id("mrel"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    source_entity_id = Column(
        Text, ForeignKey("memory_entities.id", ondelete="CASCADE"), nullable=False
    )
    target_entity_id = Column(
        Text, ForeignKey("memory_entities.id", ondelete="CASCADE"), nullable=False
    )
    relation = Column(Text, nullable=False)
    confidence_ppm = Column(BigInteger, nullable=False, default=850_000)
    source_memory_ids = Column(JSON, nullable=False, default=list)
    status = Column(Text, nullable=False, default="active")
    valid_from_ms = Column(BigInteger, nullable=True)
    valid_until_ms = Column(BigInteger, nullable=True)
    created_at_ms = Column(BigInteger, nullable=False)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "workspace",
            "source_entity_id",
            "target_entity_id",
            "relation",
            name="uq_memory_relationship",
        ),
        Index("ix_memory_relationship_source", "user_id", "workspace", "source_entity_id"),
        Index("ix_memory_relationship_target", "user_id", "workspace", "target_entity_id"),
    )


class MemorySnapshot(Base):
    __tablename__ = "memory_snapshots"

    id = Column(Text, primary_key=True, default=lambda: _id("memsnap"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    label = Column(Text, nullable=False, default="")
    memory_version = Column(BigInteger, nullable=False)
    manifest = Column(JSON, nullable=False, default=dict)
    created_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_memory_snapshot_user_workspace", "user_id", "workspace", "created_at_ms"),
    )


class MemoryBranch(Base):
    __tablename__ = "memory_branches"

    id = Column(Text, primary_key=True, default=lambda: _id("mbranch"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    name = Column(Text, nullable=False)
    from_snapshot_id = Column(
        Text, ForeignKey("memory_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    status = Column(Text, nullable=False, default="active")
    created_at_ms = Column(BigInteger, nullable=False)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "workspace", "name", name="uq_memory_branch_name"),
        Index("ix_memory_branch_user_workspace", "user_id", "workspace", "status"),
    )


class MemoryRetrievalFeedback(Base):
    __tablename__ = "memory_retrieval_feedback"

    id = Column(Text, primary_key=True, default=lambda: _id("mfb"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    memory_id = Column(Text, ForeignKey("memory_records.id", ondelete="CASCADE"), nullable=False)
    context_id = Column(Text, nullable=False)
    query_hash = Column(Text, nullable=False)
    rank = Column(BigInteger, nullable=False)
    score_ppm = Column(BigInteger, nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    helpful = Column(Boolean, nullable=True)
    outcome = Column(Text, nullable=True)
    features = Column(JSON, nullable=False, default=dict)
    created_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_memory_feedback_user_memory", "user_id", "memory_id", "created_at_ms"),
        Index("ix_memory_feedback_context", "user_id", "context_id"),
    )


class MemoryJob(Base):
    __tablename__ = "memory_jobs"

    id = Column(Text, primary_key=True, default=lambda: _id("mjob"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    job_type = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="pending")
    payload = Column(JSON, nullable=False, default=dict)
    attempts = Column(BigInteger, nullable=False, default=0)
    not_before_ms = Column(BigInteger, nullable=False)
    last_error = Column(Text, nullable=True)
    created_at_ms = Column(BigInteger, nullable=False)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_memory_job_status_due", "status", "not_before_ms"),
        Index("ix_memory_job_user_workspace", "user_id", "workspace", "status"),
    )


class MemoryEmbedding(Base):
    """Rebuildable derived-vector cache; adapters may map this to pgvector later."""

    __tablename__ = "memory_embeddings"

    memory_id = Column(Text, ForeignKey("memory_records.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    model_id = Column(Text, nullable=False)
    dimensions = Column(BigInteger, nullable=False)
    vector = Column(JSON, nullable=False, default=list)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_memory_embedding_user_workspace", "user_id", "workspace"),)


class MemoryLexicalDocument(Base):
    __tablename__ = "memory_lexical_documents"

    memory_id = Column(Text, ForeignKey("memory_records.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    token_count = Column(BigInteger, nullable=False, default=0)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_memory_lexical_doc_user_workspace", "user_id", "workspace"),)


class MemoryLexicalTerm(Base):
    __tablename__ = "memory_lexical_terms"

    id = Column(Text, primary_key=True, default=lambda: _id("mterm"))
    memory_id = Column(Text, ForeignKey("memory_records.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    term = Column(Text, nullable=False)
    term_frequency = Column(BigInteger, nullable=False, default=1)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("memory_id", "term", name="uq_memory_lexical_memory_term"),
        Index("ix_memory_lexical_term_lookup", "user_id", "workspace", "term"),
    )


class MemoryRetrievalProfile(Base):
    __tablename__ = "memory_retrieval_profiles"

    id = Column(Text, primary_key=True, default=lambda: _id("mrp"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    weights = Column(JSON, nullable=False, default=dict)
    observations = Column(BigInteger, nullable=False, default=0)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "workspace", name="uq_memory_retrieval_profile"),
        Index("ix_memory_retrieval_profile_user_workspace", "user_id", "workspace"),
    )


class MemoryConflict(Base):
    __tablename__ = "memory_conflicts"

    id = Column(Text, primary_key=True, default=lambda: _id("mconf"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    fact_key = Column(Text, nullable=False)
    left_memory_id = Column(
        Text, ForeignKey("memory_records.id", ondelete="CASCADE"), nullable=False
    )
    right_memory_id = Column(
        Text, ForeignKey("memory_records.id", ondelete="CASCADE"), nullable=False
    )
    classification = Column(Text, nullable=False, default="contradiction")
    status = Column(Text, nullable=False, default="open")
    confidence_ppm = Column(BigInteger, nullable=False, default=850_000)
    reason = Column(Text, nullable=False, default="")
    resolution = Column(Text, nullable=True)
    created_at_ms = Column(BigInteger, nullable=False)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "workspace",
            "fact_key",
            "left_memory_id",
            "right_memory_id",
            name="uq_memory_conflict_pair",
        ),
        Index("ix_memory_conflict_user_workspace_status", "user_id", "workspace", "status"),
    )


class MemoryProcedureProfile(Base):
    __tablename__ = "memory_procedure_profiles"

    memory_id = Column(Text, ForeignKey("memory_records.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    trigger = Column(Text, nullable=False, default="")
    steps = Column(JSON, nullable=False, default=list)
    verification = Column(JSON, nullable=False, default=list)
    success_count = Column(BigInteger, nullable=False, default=0)
    failure_count = Column(BigInteger, nullable=False, default=0)
    last_outcome_at_ms = Column(BigInteger, nullable=True)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_memory_procedure_user_workspace", "user_id", "workspace"),)


class MemoryFailureProfile(Base):
    __tablename__ = "memory_failure_profiles"

    memory_id = Column(Text, ForeignKey("memory_records.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace = Column(Text, nullable=False, default="")
    signature = Column(Text, nullable=False, default="")
    symptoms = Column(JSON, nullable=False, default=list)
    root_cause = Column(Text, nullable=False, default="")
    attempted_fixes = Column(JSON, nullable=False, default=list)
    successful_fix = Column(Text, nullable=False, default="")
    verification = Column(Text, nullable=False, default="")
    recurrence_count = Column(BigInteger, nullable=False, default=1)
    last_seen_at_ms = Column(BigInteger, nullable=False)
    updated_at_ms = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_memory_failure_user_workspace_signature", "user_id", "workspace", "signature"),
    )
