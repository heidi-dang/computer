"""Add Workspace OS v2 stable identity and repository checkout tables.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def _slugify(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text[:80] or "workspace"


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("slug", sa.Text(), nullable=True))
    op.add_column(
        "workspaces",
        sa.Column("workspace_type", sa.Text(), nullable=False, server_default="project"),
    )

    bind = op.get_bind()
    rows = list(
        bind.execute(
            sa.text(
                "SELECT id, user_id, name, created_at FROM workspaces "
                "ORDER BY user_id, created_at, id"
            )
        ).mappings()
    )
    seen: dict[str, set[str]] = {}
    for row in rows:
        owner_seen = seen.setdefault(str(row["user_id"]), set())
        base = _slugify(row["name"])
        candidate = base
        if candidate in owner_seen:
            suffix = str(row["id"]).replace("-", "")[:8] or "legacy"
            candidate = f"{base[: max(1, 71 - len(suffix))]}-{suffix}"
            serial = 2
            while candidate in owner_seen:
                tail = f"-{serial}"
                candidate = f"{base[: max(1, 80 - len(tail))]}{tail}"
                serial += 1
        owner_seen.add(candidate)
        bind.execute(
            sa.text("UPDATE workspaces SET slug = :slug WHERE id = :workspace_id"),
            {"slug": candidate, "workspace_id": row["id"]},
        )

    op.create_index(
        "ux_workspaces_user_slug",
        "workspaces",
        ["user_id", "slug"],
        unique=True,
    )

    op.create_table(
        "repositories",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("canonical_remote_identity", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("default_branch", sa.Text(), nullable=True),
        sa.Column("remote_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "canonical_remote_identity",
            name="uq_repository_user_remote_identity",
        ),
    )
    op.create_index("ix_repositories_user_name", "repositories", ["user_id", "name"])

    op.create_table(
        "repository_checkouts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("checkout_kind", sa.Text(), nullable=False, server_default="secondary"),
        sa.Column("canonical", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("upstream", sa.Text(), nullable=True),
        sa.Column("git_worktree_id", sa.Text(), nullable=True),
        sa.Column("last_seen_revision", sa.Text(), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "path", name="uq_repository_checkout_path"),
    )
    op.create_index(
        "ix_repository_checkouts_repository_canonical",
        "repository_checkouts",
        ["repository_id", "canonical"],
    )

    op.create_table(
        "workspace_repositories",
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="repository"),
        sa.Column("primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "repository_id"),
    )
    op.create_index(
        "ix_workspace_repositories_repository",
        "workspace_repositories",
        ["repository_id"],
    )

    op.create_table(
        "workspace_aliases",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "alias", name="uq_workspace_alias_user_alias"),
        sa.UniqueConstraint(
            "workspace_id", "alias", name="uq_workspace_alias_workspace_alias"
        ),
    )
    op.create_index(
        "ix_workspace_aliases_workspace",
        "workspace_aliases",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_aliases_workspace", table_name="workspace_aliases")
    op.drop_table("workspace_aliases")
    op.drop_index(
        "ix_workspace_repositories_repository",
        table_name="workspace_repositories",
    )
    op.drop_table("workspace_repositories")
    op.drop_index(
        "ix_repository_checkouts_repository_canonical",
        table_name="repository_checkouts",
    )
    op.drop_table("repository_checkouts")
    op.drop_index("ix_repositories_user_name", table_name="repositories")
    op.drop_table("repositories")
    op.drop_index("ux_workspaces_user_slug", table_name="workspaces")
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("workspace_type")
        batch.drop_column("slug")
