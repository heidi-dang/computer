"""Workspace and Workspace OS identity models.

Legacy workspace rows remain path-addressable for compatibility. Workspace OS v2
adds stable project, logical repository, checkout, alias, and membership
identities without changing the meaning of Workspace.path as an execution root.
"""

from __future__ import annotations

import re
import time
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Text,
    UniqueConstraint,
    delete,
    select,
)
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base
from cptr.utils.db import get_db


def _uuid() -> str:
    return str(uuid.uuid4())


def _slugify(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text[:80] or "workspace"


async def _allocate_workspace_slug(db, user_id: str, name: str, workspace_id: str) -> str:
    base = _slugify(name)
    candidate = base
    existing = set(
        (
            await db.scalars(
                select(Workspace.slug).where(
                    Workspace.user_id == user_id,
                    Workspace.slug.like(f"{base}%"),
                )
            )
        ).all()
    )
    if candidate not in existing:
        return candidate
    suffix = workspace_id.replace("-", "")[:8] or "workspace"
    candidate = f"{base[: max(1, 71 - len(suffix))]}-{suffix}"
    serial = 2
    while candidate in existing:
        tail = f"-{serial}"
        candidate = f"{base[: max(1, 80 - len(tail))]}{tail}"
        serial += 1
    return candidate


class Workspace(Base):
    """Persistent project/workspace state with legacy path compatibility."""

    __tablename__ = "workspaces"

    id = Column(Text, primary_key=True, default=_uuid)
    user_id = Column(Text, ForeignKey("users.id"), nullable=False)
    path = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    slug = Column(Text, nullable=True)
    workspace_type = Column(Text, nullable=False, default="project", server_default="project")
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "path", name="uq_workspace_user_path"),
        UniqueConstraint("user_id", "slug", name="uq_workspace_user_slug"),
    )

    @staticmethod
    async def get_by_user(user_id: str) -> list["Workspace"]:
        async with await get_db() as db:
            result = await db.execute(
                select(Workspace).where(Workspace.user_id == user_id).order_by(Workspace.created_at)
            )
            return list(result.scalars().all())

    @staticmethod
    async def get_by_path(user_id: str, path: str) -> "Workspace | None":
        async with await get_db() as db:
            result = await db.execute(
                select(Workspace).where(
                    Workspace.user_id == user_id,
                    Workspace.path == path,
                )
            )
            return result.scalar_one_or_none()

    @staticmethod
    async def get_by_slug(user_id: str, slug: str) -> "Workspace | None":
        async with await get_db() as db:
            result = await db.execute(
                select(Workspace).where(
                    Workspace.user_id == user_id,
                    Workspace.slug == slug,
                )
            )
            return result.scalar_one_or_none()

    @staticmethod
    async def upsert(user_id: str, path: str, name: str, data: dict) -> "Workspace":
        async with await get_db() as db:
            result = await db.execute(
                select(Workspace).where(
                    Workspace.user_id == user_id,
                    Workspace.path == path,
                )
            )
            ws = result.scalar_one_or_none()
            now = int(time.time())
            if ws:
                ws.name = name
                ws.data = data
                if not ws.slug:
                    ws.slug = await _allocate_workspace_slug(db, user_id, name, str(ws.id))
                ws.updated_at = now
            else:
                workspace_id = _uuid()
                ws = Workspace(
                    id=workspace_id,
                    user_id=user_id,
                    path=path,
                    name=name,
                    slug=await _allocate_workspace_slug(db, user_id, name, workspace_id),
                    data=data,
                    created_at=now,
                    updated_at=now,
                )
                db.add(ws)
            await db.commit()
            return ws

    @staticmethod
    async def archive_by_paths(user_id: str, paths: list[str]) -> int:
        if not paths:
            return 0
        async with await get_db() as db:
            result = await db.execute(
                select(Workspace).where(
                    Workspace.user_id == user_id,
                    Workspace.path.in_(paths),
                )
            )
            rows = list(result.scalars().all())
            now = int(time.time())
            for workspace in rows:
                data = dict(workspace.data or {})
                data["_cptr_archived"] = True
                workspace.data = data
                workspace.updated_at = now
            if rows:
                await db.commit()
            return len(rows)

    @staticmethod
    async def delete_by_path(user_id: str, path: str) -> bool:
        async with await get_db() as db:
            result = await db.execute(
                delete(Workspace).where(
                    Workspace.user_id == user_id,
                    Workspace.path == path,
                )
            )
            await db.commit()
            return result.rowcount > 0

    @staticmethod
    async def delete_by_paths(user_id: str, paths: list[str]) -> int:
        if not paths:
            return 0
        async with await get_db() as db:
            result = await db.execute(
                delete(Workspace).where(
                    Workspace.user_id == user_id,
                    Workspace.path.in_(paths),
                )
            )
            await db.commit()
            return result.rowcount or 0

    @staticmethod
    async def delete_by_user(user_id: str) -> None:
        async with await get_db() as db:
            await db.execute(delete(Workspace).where(Workspace.user_id == user_id))
            await db.commit()


class Repository(Base):
    """Logical source repository independent of any local checkout."""

    __tablename__ = "repositories"

    id = Column(Text, primary_key=True, default=_uuid)
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(Text, nullable=False)
    canonical_remote_identity = Column(Text, nullable=False)
    provider = Column(Text, nullable=True)
    default_branch = Column(Text, nullable=True)
    remote_metadata = Column(JSON, nullable=False, default=dict)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "canonical_remote_identity",
            name="uq_repository_user_remote_identity",
        ),
    )


class RepositoryCheckout(Base):
    """A physical checkout/worktree of a logical Repository."""

    __tablename__ = "repository_checkouts"

    id = Column(Text, primary_key=True, default=_uuid)
    repository_id = Column(
        Text,
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
    )
    path = Column(Text, nullable=False)
    checkout_kind = Column(Text, nullable=False, default="secondary", server_default="secondary")
    canonical = Column(Boolean, nullable=False, default=False, server_default="0")
    branch = Column(Text, nullable=True)
    upstream = Column(Text, nullable=True)
    git_worktree_id = Column(Text, nullable=True)
    last_seen_revision = Column(Text, nullable=True)
    available = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(BigInteger, nullable=False)
    last_seen_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("repository_id", "path", name="uq_repository_checkout_path"),
    )


class WorkspaceRepository(Base):
    """Membership of a logical Repository in a Workspace."""

    __tablename__ = "workspace_repositories"

    workspace_id = Column(
        Text,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    repository_id = Column(
        Text,
        ForeignKey("repositories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role = Column(Text, nullable=False, default="repository", server_default="repository")
    primary = Column(Boolean, nullable=False, default=False, server_default="0")
    sort_order = Column(BigInteger, nullable=False, default=0, server_default="0")
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    config = Column(JSON, nullable=False, default=dict)


class WorkspaceAlias(Base):
    """Owner-scoped stable alias for a Workspace."""

    __tablename__ = "workspace_aliases"

    id = Column(Text, primary_key=True, default=_uuid)
    workspace_id = Column(
        Text,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    alias = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "alias", name="uq_workspace_alias_user_alias"),
        UniqueConstraint("workspace_id", "alias", name="uq_workspace_alias_workspace_alias"),
    )
