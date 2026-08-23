"""Initial schema: users, auths, user_states, config, files, chat, chat_message.

Revision ID: 0001
Create Date: 2026-05-28
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("display_name", sa.Text, nullable=True),
        sa.Column("profile_image_url", sa.Text, nullable=True),
        sa.Column("role", sa.Text, nullable=False, server_default="pending"),
        sa.Column("settings", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=True),
        sa.Column("last_seen_at", sa.BigInteger, nullable=True),
    )

    op.create_table(
        "auths",
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("username", sa.Text, unique=True, nullable=False),
        sa.Column("password", sa.Text, nullable=True),
    )

    op.create_table(
        "user_states",
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("data", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.BigInteger, nullable=True),
    )

    op.create_table(
        "config",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=True),
    )

    op.create_table(
        "files",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("hash", sa.Text, nullable=False),
        sa.Column("meta", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("data", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=True),
    )

    op.create_table(
        "chat",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("current_message_id", sa.Text, nullable=True),
        sa.Column("meta", sa.JSON, nullable=True),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=False),
    )

    op.create_table(
        "chat_message",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "chat_id",
            sa.Text,
            sa.ForeignKey("chat.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("parent_id", sa.Text, nullable=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("done", sa.Boolean, default=True),
        sa.Column("output", sa.JSON, nullable=True),
        sa.Column("usage", sa.JSON, nullable=True),
        sa.Column("meta", sa.JSON, nullable=True),
        sa.Column("created_at", sa.BigInteger, nullable=False),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("data", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=True),
        sa.UniqueConstraint("user_id", "path", name="uq_workspace_user_path"),
    )


def downgrade() -> None:
    op.drop_table("workspaces")
    op.drop_table("chat_message")
    op.drop_table("chat")
    op.drop_table("files")
    op.drop_table("config")
    op.drop_table("user_states")
    op.drop_table("auths")
    op.drop_table("users")
