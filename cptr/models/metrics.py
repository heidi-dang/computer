"""Durable MCP usage, observed engineering metrics, and coding benchmark records."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _benchmark_id() -> str:
    return f"bench_{uuid.uuid4().hex}"


class McpUsageEvent(Base):
    """Immutable, restart-safe source of truth for one MCP-visible usage event."""

    __tablename__ = "mcp_usage_events"

    id = Column(Text, primary_key=True)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    timestamp_ms = Column(BigInteger, nullable=False)
    request_id = Column(Text, nullable=True)
    correlation_id = Column(Text, nullable=True)
    session_id = Column(Text, nullable=True)
    client_id = Column(Text, nullable=False)
    model_reported = Column(Text, nullable=True)
    model_canonical = Column(Text, nullable=True)
    model_source = Column(Text, nullable=False)
    tool_name = Column(Text, nullable=False)
    input_tokens_estimated = Column(BigInteger, nullable=False, default=0)
    output_tokens_estimated = Column(BigInteger, nullable=False, default=0)
    estimator_method = Column(Text, nullable=False)
    estimator_exact_for_model = Column(BigInteger, nullable=False, default=0)
    status = Column(Text, nullable=False)
    pricing_status = Column(Text, nullable=False)
    pricing_version = Column(Text, nullable=False)
    input_usd_per_million = Column(Text, nullable=True)
    cached_input_usd_per_million = Column(Text, nullable=True)
    output_usd_per_million = Column(Text, nullable=True)
    input_cost_pico_usd = Column(BigInteger, nullable=False, default=0)
    output_cost_pico_usd = Column(BigInteger, nullable=False, default=0)
    simulated_cost_pico_usd = Column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        Index("ix_mcp_usage_user_timestamp", "user_id", "timestamp_ms"),
        Index("ix_mcp_usage_user_model_timestamp", "user_id", "model_canonical", "timestamp_ms"),
        Index("ix_mcp_usage_user_session_timestamp", "user_id", "session_id", "timestamp_ms"),
    )


class McpEngineeringSession(Base):
    """Payload-free operational aggregate for one observed MCP coding session."""

    __tablename__ = "mcp_engineering_sessions"

    id = Column(Text, primary_key=True, default=_uuid)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    session_key = Column(Text, nullable=False)
    session_id = Column(Text, nullable=True)
    client_id = Column(Text, nullable=False)
    model_key = Column(Text, nullable=False)
    model_reported = Column(Text, nullable=True)
    model_canonical = Column(Text, nullable=True)
    first_seen_ms = Column(BigInteger, nullable=False)
    last_seen_ms = Column(BigInteger, nullable=False)
    tool_calls = Column(BigInteger, nullable=False, default=0)
    successful_tool_calls = Column(BigInteger, nullable=False, default=0)
    failed_tool_calls = Column(BigInteger, nullable=False, default=0)
    coding_mutations = Column(BigInteger, nullable=False, default=0)
    verification_calls = Column(BigInteger, nullable=False, default=0)
    read_calls = Column(BigInteger, nullable=False, default=0)
    input_tokens_estimated = Column(BigInteger, nullable=False, default=0)
    output_tokens_estimated = Column(BigInteger, nullable=False, default=0)
    simulated_cost_pico_usd = Column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "session_key", "model_key", name="uq_mcp_engineering_user_session_model"
        ),
        Index("ix_mcp_engineering_user_last_seen", "user_id", "last_seen_ms"),
        Index("ix_mcp_engineering_user_model", "user_id", "model_key", "last_seen_ms"),
    )


class CodingBenchmarkRun(Base):
    """Owner-scoped lifecycle and objective evidence for a standardized coding run."""

    __tablename__ = "coding_benchmark_runs"

    id = Column(Text, primary_key=True, default=_benchmark_id)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    suite_id = Column(Text, nullable=False)
    suite_version = Column(Text, nullable=False)
    model_reported = Column(Text, nullable=True)
    model_canonical = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="READY")
    workspace_id = Column(Text, ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)
    workspace_path = Column(Text, nullable=False)
    grader_seed = Column(Text, nullable=False)
    score = Column(BigInteger, nullable=True)
    max_score = Column(BigInteger, nullable=False, default=100)
    case_results = Column(JSON, nullable=False, default=list)
    error_summary = Column(Text, nullable=True)
    started_at_ms = Column(BigInteger, nullable=False)
    completed_at_ms = Column(BigInteger, nullable=True)
    duration_ms = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_coding_benchmark_user_started", "user_id", "started_at_ms"),
        Index(
            "ix_coding_benchmark_user_suite_model",
            "user_id",
            "suite_id",
            "suite_version",
            "model_canonical",
        ),
    )
