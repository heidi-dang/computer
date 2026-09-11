"""Stable Workspace OS references with legacy path compatibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select

from cptr.models.workspaces import Workspace, WorkspaceAlias
from cptr.utils.db import get_db


class WorkspaceRefError(ValueError):
    code = "WORKSPACE_REF_ERROR"


class WorkspaceRefNotFound(WorkspaceRefError):
    code = "WORKSPACE_REF_NOT_FOUND"


class WorkspaceRefAmbiguous(WorkspaceRefError):
    code = "WORKSPACE_REF_AMBIGUOUS"

    def __init__(self, reference: str, candidates: list[dict[str, str | None]]) -> None:
        super().__init__(f"workspace reference is ambiguous: {reference}")
        self.reference = reference
        self.candidates = candidates


@dataclass(frozen=True)
class WorkspaceRefResolution:
    workspace: Workspace
    matched_by: str


def _archived(workspace: Workspace) -> bool:
    data = workspace.data if isinstance(workspace.data, dict) else {}
    return bool(data.get("_cptr_archived"))


def _candidate(workspace: Workspace) -> dict[str, str | None]:
    return {
        "workspace_id": str(workspace.id),
        "slug": str(workspace.slug) if workspace.slug else None,
        "name": str(workspace.name),
        "path": str(workspace.path),
    }


def choose_workspace_ref(
    *,
    reference: str,
    workspaces: Iterable[Workspace],
    aliases: Iterable[WorkspaceAlias] = (),
    include_archived: bool = False,
) -> WorkspaceRefResolution:
    """Resolve a stable/legacy workspace reference without fuzzy guessing."""

    ref = str(reference or "").strip()
    if not ref:
        raise WorkspaceRefNotFound("workspace reference is blank")

    candidates = [ws for ws in workspaces if include_archived or not _archived(ws)]

    for matched_by, predicate in (
        ("id", lambda ws: str(ws.id) == ref),
        ("slug", lambda ws: bool(ws.slug) and str(ws.slug) == ref),
    ):
        matches = [ws for ws in candidates if predicate(ws)]
        if len(matches) == 1:
            return WorkspaceRefResolution(matches[0], matched_by)
        if len(matches) > 1:
            raise WorkspaceRefAmbiguous(ref, [_candidate(ws) for ws in matches])

    alias_workspace_ids = {
        str(alias.workspace_id) for alias in aliases if str(alias.alias or "").strip() == ref
    }
    if alias_workspace_ids:
        matches = [ws for ws in candidates if str(ws.id) in alias_workspace_ids]
        if len(matches) == 1:
            return WorkspaceRefResolution(matches[0], "alias")
        if len(matches) > 1:
            raise WorkspaceRefAmbiguous(ref, [_candidate(ws) for ws in matches])

    path_matches = [ws for ws in candidates if str(ws.path) == ref]
    if len(path_matches) == 1:
        return WorkspaceRefResolution(path_matches[0], "legacy_path")
    if len(path_matches) > 1:
        raise WorkspaceRefAmbiguous(ref, [_candidate(ws) for ws in path_matches])

    name_matches = [ws for ws in candidates if str(ws.name) == ref]
    if len(name_matches) == 1:
        return WorkspaceRefResolution(name_matches[0], "legacy_name")
    if len(name_matches) > 1:
        raise WorkspaceRefAmbiguous(ref, [_candidate(ws) for ws in name_matches])

    raise WorkspaceRefNotFound(f"workspace not found: {ref}")


async def resolve_workspace_ref(
    *,
    user_id: str,
    reference: str,
    include_archived: bool = False,
) -> WorkspaceRefResolution:
    workspaces = await Workspace.get_by_user(user_id)
    async with await get_db() as db:
        aliases = list(
            (
                await db.scalars(select(WorkspaceAlias).where(WorkspaceAlias.user_id == user_id))
            ).all()
        )
    return choose_workspace_ref(
        reference=reference,
        workspaces=workspaces,
        aliases=aliases,
        include_archived=include_archived,
    )
