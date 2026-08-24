"""persist parallel Build node ownership and integration metadata

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "flowdeck_build_nodes" not in inspector.get_table_names():
        op.create_table(
            "flowdeck_build_nodes",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("run_id", sa.Text(), nullable=False),
            sa.Column("node_key", sa.Text(), nullable=False),
            sa.Column("role", sa.Text(), nullable=True),
            sa.Column("dependencies", sa.JSON(), nullable=False),
            sa.Column("mutation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("workspace", sa.Text(), nullable=False),
            sa.Column("worktree", sa.Text(), nullable=True),
            sa.Column("branch", sa.Text(), nullable=True),
            sa.Column("common_base", sa.Text(), nullable=True),
            sa.Column("overlap_paths", sa.JSON(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("owner", sa.Text(), nullable=True),
            sa.Column("fencing_epoch", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("authoritative_evidence", sa.JSON(), nullable=True),
            sa.Column("integration_status", sa.Text(), nullable=False, server_default="PENDING"),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
            sa.UniqueConstraint("run_id", "node_key", name="uq_flowdeck_build_node_key"),
        )
        op.create_index("ix_flowdeck_build_nodes_run_id", "flowdeck_build_nodes", ["run_id"])
        return

    existing = {column["name"] for column in inspector.get_columns("flowdeck_build_nodes")}
    additions = (
        ("role", sa.Text(), None),
        ("branch", sa.Text(), None),
        ("owner", sa.Text(), None),
        ("fencing_epoch", sa.Integer(), "0"),
        ("authoritative_evidence", sa.JSON(), None),
        ("integration_status", sa.Text(), "PENDING"),
        ("retry_count", sa.Integer(), "0"),
    )
    with op.batch_alter_table("flowdeck_build_nodes") as batch:
        for name, column_type, default in additions:
            if name not in existing:
                kwargs = {"nullable": name not in {"fencing_epoch", "integration_status", "retry_count"}}
                if default is not None:
                    kwargs["server_default"] = default
                batch.add_column(sa.Column(name, column_type, **kwargs))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "flowdeck_build_nodes" in inspector.get_table_names():
        op.drop_index("ix_flowdeck_build_nodes_run_id", table_name="flowdeck_build_nodes")
        op.drop_table("flowdeck_build_nodes")
