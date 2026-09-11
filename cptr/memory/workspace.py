"""Workspace namespace and legacy alias resolution for CPTR Memory Core."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select

from cptr.models.workspaces import Workspace
from cptr.utils.db import get_db

# Short in-process cache to avoid repeating DB lookups for identical (user_id, workspace) pairs:
# (user_id, workspace_key) -> (timestamp, WorkspaceNamespace)
_NAMESPACE_CACHE: dict[tuple[str, str], tuple[float, WorkspaceNamespace]] = {}
_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class WorkspaceNamespace:
    """Resolved workspace namespace with stable UUID and legacy alias compatibility."""

    workspace_id: str  # Canonical stable workspace UUID, or "" for user/global memory
    workspace_path: str  # Local filesystem path if known, else ""
    aliases: tuple[str, ...]  # Compatible identifiers (UUID, path, name, slug)
    is_user_scope: bool
    name: str = ""
    slug: str = ""

    def matches(self, candidate: str | None) -> bool:
        cand = str(candidate or "").strip()
        if self.is_user_scope:
            return cand == ""
        return cand in self.aliases


def clear_workspace_namespace_cache() -> None:
    """Clear in-memory workspace namespace resolution cache."""
    _NAMESPACE_CACHE.clear()


async def resolve_workspace_namespace(
    user_id: str,
    workspace: str | None,
    *,
    db: Any | None = None,
    session_factory: Any | None = None,
) -> WorkspaceNamespace:
    """Resolve a workspace string (UUID, path, name, slug) into a canonical WorkspaceNamespace."""
    user_key = str(user_id or "").strip()
    ws_str = str(workspace or "").strip()

    if not ws_str:
        return WorkspaceNamespace(
            workspace_id="",
            workspace_path="",
            aliases=("",),
            is_user_scope=True,
        )

    cache_key = (user_key, ws_str)
    now = time.monotonic()
    cached = _NAMESPACE_CACHE.get(cache_key)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    async def _lookup(session) -> Workspace | None:
        predicates = [
            Workspace.id == ws_str,
            Workspace.path == ws_str,
            Workspace.name == ws_str,
        ]
        if hasattr(Workspace, "slug"):
            predicates.append(getattr(Workspace, "slug") == ws_str)
        stmt = (
            select(Workspace)
            .where(
                Workspace.user_id == user_key,
                or_(*predicates),
            )
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    row: Workspace | None = None
    try:
        if db is not None:
            row = await _lookup(db)
        else:
            factory = session_factory or get_db
            if callable(factory):
                session_cm = factory()
                if hasattr(session_cm, "__aenter__"):
                    async with session_cm as session:
                        row = await _lookup(session)
                else:
                    async with await factory() as session:
                        row = await _lookup(session)
            elif hasattr(factory, "__aenter__"):
                async with factory as session:
                    row = await _lookup(session)
    except Exception:
        row = None

    if row is not None:
        canonical_id = str(row.id or "").strip()
        path = str(row.path or "").strip()
        name = str(row.name or "").strip()
        slug = str(getattr(row, "slug", "") or "").strip()
        raw_aliases = [canonical_id, path, name, slug, ws_str]
        seen: set[str] = set()
        aliases_list: list[str] = []
        for item in raw_aliases:
            if item and item not in seen:
                seen.add(item)
                aliases_list.append(item)
        ns = WorkspaceNamespace(
            workspace_id=canonical_id,
            workspace_path=path,
            aliases=tuple(aliases_list),
            is_user_scope=False,
            name=name,
            slug=slug,
        )
    else:
        path = ws_str if ("/" in ws_str or "\\" in ws_str) else ""
        ns = WorkspaceNamespace(
            workspace_id=ws_str,
            workspace_path=path,
            aliases=(ws_str,),
            is_user_scope=False,
            name="",
            slug="",
        )

    _NAMESPACE_CACHE[cache_key] = (now, ns)
    return ns
