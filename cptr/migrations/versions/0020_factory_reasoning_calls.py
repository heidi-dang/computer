"""Add durable Dark Factory reasoning call history.

Revision ID: 0020
Revises: 0019

Compatibility note: ``main`` briefly used revision ``0019`` for the browser
broker while the Dark Factory branch used the same ID for its core schema.  A
legacy-main database can therefore report ``0019`` while lacking the factory
core tables.  Repair that historical ambiguity before creating reasoning rows.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def _ensure_factory_core_compat() -> None:
    if "factory_runs" in set(sa.inspect(op.get_bind()).get_table_names()):
        return

    migration_path = Path(__file__).with_name("0019_dark_factory_core.py")
    spec = importlib.util.spec_from_file_location("_cptr_dark_factory_core_compat", migration_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load Dark Factory core compatibility migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.upgrade()


def upgrade() -> None:
    _ensure_factory_core_compat()
    op.create_table(
        "factory_reasoning_calls",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("factory_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "cycle_id",
            sa.Text(),
            sa.ForeignKey("factory_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("role_ordinal", sa.BigInteger(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("response_id", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("runtime_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("data", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("provider_metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "cycle_id",
            "role",
            "role_ordinal",
            name="uq_factory_reasoning_cycle_role_ordinal",
        ),
    )
    op.create_index(
        "ix_factory_reasoning_run_cycle_role",
        "factory_reasoning_calls",
        ["run_id", "cycle_id", "role", "role_ordinal"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_factory_reasoning_run_cycle_role",
        table_name="factory_reasoning_calls",
    )
    op.drop_table("factory_reasoning_calls")
