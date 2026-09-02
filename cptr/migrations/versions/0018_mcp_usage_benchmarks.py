"""Persist MCP usage, observed engineering sessions, and coding benchmark runs.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_usage_events",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("model_reported", sa.Text(), nullable=True),
        sa.Column("model_canonical", sa.Text(), nullable=True),
        sa.Column("model_source", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("input_tokens_estimated", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens_estimated", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("estimator_method", sa.Text(), nullable=False),
        sa.Column("estimator_exact_for_model", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("pricing_status", sa.Text(), nullable=False),
        sa.Column("pricing_version", sa.Text(), nullable=False),
        sa.Column("input_usd_per_million", sa.Text(), nullable=True),
        sa.Column("cached_input_usd_per_million", sa.Text(), nullable=True),
        sa.Column("output_usd_per_million", sa.Text(), nullable=True),
        sa.Column("input_cost_pico_usd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_cost_pico_usd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("simulated_cost_pico_usd", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_index("ix_mcp_usage_user_timestamp", "mcp_usage_events", ["user_id", "timestamp_ms"])
    op.create_index(
        "ix_mcp_usage_user_model_timestamp",
        "mcp_usage_events",
        ["user_id", "model_canonical", "timestamp_ms"],
    )
    op.create_index(
        "ix_mcp_usage_user_session_timestamp",
        "mcp_usage_events",
        ["user_id", "session_id", "timestamp_ms"],
    )

    op.create_table(
        "mcp_engineering_sessions",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("session_key", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("model_key", sa.Text(), nullable=False),
        sa.Column("model_reported", sa.Text(), nullable=True),
        sa.Column("model_canonical", sa.Text(), nullable=True),
        sa.Column("first_seen_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_ms", sa.BigInteger(), nullable=False),
        sa.Column("tool_calls", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("successful_tool_calls", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failed_tool_calls", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("coding_mutations", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("verification_calls", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("read_calls", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("input_tokens_estimated", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens_estimated", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("simulated_cost_pico_usd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "user_id", "session_key", "model_key", name="uq_mcp_engineering_user_session_model"
        ),
    )
    op.create_index(
        "ix_mcp_engineering_user_last_seen",
        "mcp_engineering_sessions",
        ["user_id", "last_seen_ms"],
    )
    op.create_index(
        "ix_mcp_engineering_user_model",
        "mcp_engineering_sessions",
        ["user_id", "model_key", "last_seen_ms"],
    )

    op.create_table(
        "coding_benchmark_runs",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("suite_id", sa.Text(), nullable=False),
        sa.Column("suite_version", sa.Text(), nullable=False),
        sa.Column("model_reported", sa.Text(), nullable=True),
        sa.Column("model_canonical", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="READY"),
        sa.Column(
            "workspace_id",
            sa.Text(),
            sa.ForeignKey("workspaces.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("grader_seed", sa.Text(), nullable=False),
        sa.Column("score", sa.BigInteger(), nullable=True),
        sa.Column("max_score", sa.BigInteger(), nullable=False, server_default="100"),
        sa.Column("case_results", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_coding_benchmark_user_started", "coding_benchmark_runs", ["user_id", "started_at_ms"]
    )
    op.create_index(
        "ix_coding_benchmark_user_suite_model",
        "coding_benchmark_runs",
        ["user_id", "suite_id", "suite_version", "model_canonical"],
    )


def downgrade() -> None:
    op.drop_index("ix_coding_benchmark_user_suite_model", table_name="coding_benchmark_runs")
    op.drop_index("ix_coding_benchmark_user_started", table_name="coding_benchmark_runs")
    op.drop_table("coding_benchmark_runs")
    op.drop_index("ix_mcp_engineering_user_model", table_name="mcp_engineering_sessions")
    op.drop_index("ix_mcp_engineering_user_last_seen", table_name="mcp_engineering_sessions")
    op.drop_table("mcp_engineering_sessions")
    op.drop_index("ix_mcp_usage_user_session_timestamp", table_name="mcp_usage_events")
    op.drop_index("ix_mcp_usage_user_model_timestamp", table_name="mcp_usage_events")
    op.drop_index("ix_mcp_usage_user_timestamp", table_name="mcp_usage_events")
    op.drop_table("mcp_usage_events")
