"""Add stable Workspace OS identity to chats.

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("workspace_id", sa.Text(), nullable=True))
    op.create_index("ix_chats_workspace_id", "chats", ["workspace_id"])
    # Backfill only unambiguous owner+path matches. Legacy meta.workspace remains
    # intact for compatibility and filesystem chat discovery.
    bind = op.get_bind()
    rows = list(
        bind.execute(
            sa.text(
                "SELECT c.id AS chat_id, c.user_id AS user_id, "
                "json_extract(c.meta, '$.workspace') AS workspace_path "
                "FROM chats AS c WHERE c.workspace_id IS NULL AND c.meta IS NOT NULL"
            )
        ).mappings()
    )
    for row in rows:
        path = row["workspace_path"]
        if not path:
            continue
        matches = list(
            bind.execute(
                sa.text(
                    "SELECT id FROM workspaces "
                    "WHERE user_id = :user_id AND path = :path ORDER BY created_at, id"
                ),
                {"user_id": row["user_id"], "path": path},
            ).scalars()
        )
        if len(matches) != 1:
            continue
        bind.execute(
            sa.text("UPDATE chats SET workspace_id = :workspace_id WHERE id = :chat_id"),
            {"workspace_id": matches[0], "chat_id": row["chat_id"]},
        )

    with op.batch_alter_table("chats") as batch:
        batch.create_foreign_key(
            "fk_chats_workspace_id",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("chats") as batch:
        batch.drop_constraint("fk_chats_workspace_id", type_="foreignkey")
    op.drop_index("ix_chats_workspace_id", table_name="chats")
    with op.batch_alter_table("chats") as batch:
        batch.drop_column("workspace_id")
