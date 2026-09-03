"""Add advanced derived indexes and memory intelligence persistence.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_retrieval_feedback",
        sa.Column("features", sa.JSON(), nullable=False, server_default="{}"),
    )

    op.create_table(
        "memory_lexical_documents",
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
        sa.Column("token_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_memory_lexical_doc_user_workspace",
        "memory_lexical_documents",
        ["user_id", "workspace"],
    )

    op.create_table(
        "memory_lexical_terms",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "memory_id",
            sa.Text(),
            sa.ForeignKey("memory_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("term_frequency", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("memory_id", "term", name="uq_memory_lexical_memory_term"),
    )
    op.create_index(
        "ix_memory_lexical_term_lookup",
        "memory_lexical_terms",
        ["user_id", "workspace", "term"],
    )

    op.create_table(
        "memory_retrieval_profiles",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column("weights", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("observations", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("user_id", "workspace", name="uq_memory_retrieval_profile"),
    )
    op.create_index(
        "ix_memory_retrieval_profile_user_workspace",
        "memory_retrieval_profiles",
        ["user_id", "workspace"],
    )

    op.create_table(
        "memory_conflicts",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace", sa.Text(), nullable=False, server_default=""),
        sa.Column("fact_key", sa.Text(), nullable=False),
        sa.Column(
            "left_memory_id",
            sa.Text(),
            sa.ForeignKey("memory_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "right_memory_id",
            sa.Text(),
            sa.ForeignKey("memory_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("classification", sa.Text(), nullable=False, server_default="contradiction"),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("confidence_ppm", sa.BigInteger(), nullable=False, server_default="850000"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "workspace",
            "fact_key",
            "left_memory_id",
            "right_memory_id",
            name="uq_memory_conflict_pair",
        ),
    )
    op.create_index(
        "ix_memory_conflict_user_workspace_status",
        "memory_conflicts",
        ["user_id", "workspace", "status"],
    )

    op.create_table(
        "memory_procedure_profiles",
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
        sa.Column("trigger", sa.Text(), nullable=False, server_default=""),
        sa.Column("steps", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("verification", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("success_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_outcome_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_memory_procedure_user_workspace",
        "memory_procedure_profiles",
        ["user_id", "workspace"],
    )

    op.create_table(
        "memory_failure_profiles",
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
        sa.Column("signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("symptoms", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("root_cause", sa.Text(), nullable=False, server_default=""),
        sa.Column("attempted_fixes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("successful_fix", sa.Text(), nullable=False, server_default=""),
        sa.Column("verification", sa.Text(), nullable=False, server_default=""),
        sa.Column("recurrence_count", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("last_seen_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_memory_failure_user_workspace_signature",
        "memory_failure_profiles",
        ["user_id", "workspace", "signature"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_failure_user_workspace_signature", table_name="memory_failure_profiles"
    )
    op.drop_table("memory_failure_profiles")
    op.drop_index("ix_memory_procedure_user_workspace", table_name="memory_procedure_profiles")
    op.drop_table("memory_procedure_profiles")
    op.drop_index("ix_memory_conflict_user_workspace_status", table_name="memory_conflicts")
    op.drop_table("memory_conflicts")
    op.drop_index(
        "ix_memory_retrieval_profile_user_workspace", table_name="memory_retrieval_profiles"
    )
    op.drop_table("memory_retrieval_profiles")
    op.drop_index("ix_memory_lexical_term_lookup", table_name="memory_lexical_terms")
    op.drop_table("memory_lexical_terms")
    op.drop_index("ix_memory_lexical_doc_user_workspace", table_name="memory_lexical_documents")
    op.drop_table("memory_lexical_documents")
    op.drop_column("memory_retrieval_feedback", "features")
