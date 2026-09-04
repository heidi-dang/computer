"""Add canonical embedded Memory Core persistence.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_namespace_state",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("active_snapshot_id", sa.Text(), nullable=True),
        sa.Column("active_branch_id", sa.Text(), nullable=True),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("user_id", "workspace", name="uq_memory_namespace_user_workspace"),
    )
    op.create_index(
        "ix_memory_namespace_user_workspace",
        "memory_namespace_state",
        ["user_id", "workspace"],
    )

    op.create_table(
        "memory_records",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, server_default="semantic"),
        sa.Column("canonical_text", sa.Text(), nullable=False),
        sa.Column("structured_value", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("confidence_ppm", sa.BigInteger(), nullable=False, server_default="850000"),
        sa.Column("importance_ppm", sa.BigInteger(), nullable=False, server_default="500000"),
        sa.Column("trust_level", sa.Text(), nullable=False, server_default="agent_observation"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("valid_from_ms", sa.BigInteger(), nullable=True),
        sa.Column("valid_until_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "superseded_by_id",
            sa.Text(),
            sa.ForeignKey("memory_records.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_event_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("branch_id", sa.Text(), nullable=True),
        sa.Column(
            "parent_memory_id",
            sa.Text(),
            sa.ForeignKey("memory_records.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("verified_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("verification_expires_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("access_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_accessed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_memory_record_user_workspace_status",
        "memory_records",
        ["user_id", "workspace", "status"],
    )
    op.create_index(
        "ix_memory_record_user_hash", "memory_records", ["user_id", "workspace", "content_hash"]
    )
    op.create_index(
        "ix_memory_record_user_kind", "memory_records", ["user_id", "workspace", "kind", "status"]
    )
    op.create_index(
        "ix_memory_record_user_branch",
        "memory_records",
        ["user_id", "workspace", "branch_id", "status"],
    )

    op.create_table(
        "memory_checkpoints",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column("task_key", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("memory_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "parent_checkpoint_id",
            sa.Text(),
            sa.ForeignKey("memory_checkpoints.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "user_id", "workspace", "task_key", "version", name="uq_memory_checkpoint_task_version"
        ),
    )
    op.create_index(
        "ix_memory_checkpoint_task",
        "memory_checkpoints",
        ["user_id", "workspace", "task_key", "version"],
    )

    op.create_table(
        "memory_entities",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False, server_default="concept"),
        sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("valid_from_ms", sa.BigInteger(), nullable=True),
        sa.Column("valid_until_ms", sa.BigInteger(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "workspace",
            "normalized_name",
            "entity_type",
            name="uq_memory_entity_name_type",
        ),
    )
    op.create_index(
        "ix_memory_entity_user_workspace", "memory_entities", ["user_id", "workspace", "status"]
    )

    op.create_table(
        "memory_relationships",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "source_entity_id",
            sa.Text(),
            sa.ForeignKey("memory_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_entity_id",
            sa.Text(),
            sa.ForeignKey("memory_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("confidence_ppm", sa.BigInteger(), nullable=False, server_default="850000"),
        sa.Column("source_memory_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("valid_from_ms", sa.BigInteger(), nullable=True),
        sa.Column("valid_until_ms", sa.BigInteger(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "workspace",
            "source_entity_id",
            "target_entity_id",
            "relation",
            name="uq_memory_relationship",
        ),
    )
    op.create_index(
        "ix_memory_relationship_source",
        "memory_relationships",
        ["user_id", "workspace", "source_entity_id"],
    )
    op.create_index(
        "ix_memory_relationship_target",
        "memory_relationships",
        ["user_id", "workspace", "target_entity_id"],
    )

    op.create_table(
        "memory_snapshots",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("memory_version", sa.BigInteger(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_memory_snapshot_user_workspace",
        "memory_snapshots",
        ["user_id", "workspace", "created_at_ms"],
    )

    op.create_table(
        "memory_branches",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "from_snapshot_id",
            sa.Text(),
            sa.ForeignKey("memory_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("user_id", "workspace", "name", name="uq_memory_branch_name"),
    )
    op.create_index(
        "ix_memory_branch_user_workspace", "memory_branches", ["user_id", "workspace", "status"]
    )

    op.create_table(
        "memory_retrieval_feedback",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "memory_id",
            sa.Text(),
            sa.ForeignKey("memory_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("context_id", sa.Text(), nullable=False),
        sa.Column("query_hash", sa.Text(), nullable=False),
        sa.Column("rank", sa.BigInteger(), nullable=False),
        sa.Column("score_ppm", sa.BigInteger(), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("helpful", sa.Boolean(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_memory_feedback_user_memory",
        "memory_retrieval_feedback",
        ["user_id", "memory_id", "created_at_ms"],
    )
    op.create_index(
        "ix_memory_feedback_context", "memory_retrieval_feedback", ["user_id", "context_id"]
    )

    op.create_table(
        "memory_jobs",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("attempts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("not_before_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_memory_job_status_due", "memory_jobs", ["status", "not_before_ms"])
    op.create_index(
        "ix_memory_job_user_workspace", "memory_jobs", ["user_id", "workspace", "status"]
    )

    op.create_table(
        "memory_embeddings",
        sa.Column(
            "memory_id",
            sa.Text(),
            sa.ForeignKey("memory_records.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.BigInteger(), nullable=False),
        sa.Column("vector", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_memory_embedding_user_workspace", "memory_embeddings", ["user_id", "workspace"]
    )


def downgrade() -> None:
    op.drop_index("ix_memory_embedding_user_workspace", table_name="memory_embeddings")
    op.drop_table("memory_embeddings")
    op.drop_index("ix_memory_job_user_workspace", table_name="memory_jobs")
    op.drop_index("ix_memory_job_status_due", table_name="memory_jobs")
    op.drop_table("memory_jobs")
    op.drop_index("ix_memory_feedback_context", table_name="memory_retrieval_feedback")
    op.drop_index("ix_memory_feedback_user_memory", table_name="memory_retrieval_feedback")
    op.drop_table("memory_retrieval_feedback")
    op.drop_index("ix_memory_branch_user_workspace", table_name="memory_branches")
    op.drop_table("memory_branches")
    op.drop_index("ix_memory_snapshot_user_workspace", table_name="memory_snapshots")
    op.drop_table("memory_snapshots")
    op.drop_index("ix_memory_relationship_target", table_name="memory_relationships")
    op.drop_index("ix_memory_relationship_source", table_name="memory_relationships")
    op.drop_table("memory_relationships")
    op.drop_index("ix_memory_entity_user_workspace", table_name="memory_entities")
    op.drop_table("memory_entities")
    op.drop_index("ix_memory_checkpoint_task", table_name="memory_checkpoints")
    op.drop_table("memory_checkpoints")
    op.drop_index("ix_memory_record_user_branch", table_name="memory_records")
    op.drop_index("ix_memory_record_user_kind", table_name="memory_records")
    op.drop_index("ix_memory_record_user_hash", table_name="memory_records")
    op.drop_index("ix_memory_record_user_workspace_status", table_name="memory_records")
    op.drop_table("memory_records")
    op.drop_index("ix_memory_namespace_user_workspace", table_name="memory_namespace_state")
    op.drop_table("memory_namespace_state")
