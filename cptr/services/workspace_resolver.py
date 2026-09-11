"""Workspace resolver service with UUID/slug/alias/path/exact-name ambiguity semantics.

Deterministic workspace resolution hierarchy:
1. Exact Workspace ID (UUID / primary key)
2. Unique workspace slug (explicit in workspace attribute or data)
3. Unique workspace alias (explicit in workspace, aliases, or configuration)
4. Legacy exact filesystem path (preserving absolute/resolved path lookups, spaces, quotes, and file:// URIs)
5. Exact workspace name (detecting duplicate names and raising explicit ambiguity errors)
6. Derived slug match (slugified name fallback)
7. Repository basename match (directory name from path)
8. Cross-repo / fuzzy intent match (prohibited for destructive/admin operations)

Ambiguity is surfaced explicitly with candidate metadata whenever multiple workspaces match
at the same precedence level, rather than silently choosing an arbitrary target.
"""

from __future__ import annotations

import re
import urllib.parse
from enum import Enum
from pathlib import Path
from typing import Any, Sequence


class ResolutionStage(str, Enum):
    UUID = "uuid"
    EXACT_PATH = "exact_path"
    EXACT_NAME = "exact_name"
    SLUG = "slug"
    ALIAS = "alias"
    REPO_MATCH = "repo_match"
    FUZZY = "fuzzy"


class WorkspaceResolverError(Exception):
    """Base exception for workspace resolution failures."""


class WorkspaceNotFoundError(WorkspaceResolverError):
    """Raised when no workspace matches the query."""

    def __init__(self, message: str, *, query: str) -> None:
        super().__init__(message)
        self.query = query


class AmbiguousWorkspaceError(WorkspaceResolverError):
    """Raised when multiple workspaces match a query at the same precedence level."""

    def __init__(
        self,
        message: str,
        *,
        query: str,
        candidates: Sequence[Any],
        stage: ResolutionStage | str,
    ) -> None:
        super().__init__(message)
        self.query = query
        self.candidates = list(candidates)
        self.stage = stage.value if isinstance(stage, ResolutionStage) else str(stage)


class UnsafeResolutionError(WorkspaceResolverError):
    """Raised when a destructive operation attempts to resolve using an unsafe/fuzzy match."""

    def __init__(
        self,
        message: str,
        *,
        query: str,
        candidates: Sequence[Any],
    ) -> None:
        super().__init__(message)
        self.query = query
        self.candidates = list(candidates)


class ResolutionResult:
    """Detailed result of workspace resolution."""

    def __init__(
        self,
        workspace: Any,
        stage: ResolutionStage,
        query: str,
        confidence: float,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.stage = stage
        self.query = query
        self.confidence = confidence
        self.metadata = metadata or {}

    def __repr__(self) -> str:
        ws_id = _get_id(self.workspace)
        ws_name = _get_name(self.workspace)
        return (
            f"ResolutionResult(id={ws_id!r}, name={ws_name!r}, "
            f"stage={self.stage.value!r}, confidence={self.confidence})"
        )


def slugify(value: str | None) -> str:
    """Normalize a string into a clean lowercase slug."""
    if not value:
        return ""
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    return normalized.strip("-")


def _get_id(ws: Any) -> str:
    if isinstance(ws, dict):
        return str(ws.get("id") or "").strip()
    return str(getattr(ws, "id", None) or "").strip()


def _get_name(ws: Any) -> str:
    if isinstance(ws, dict):
        return str(ws.get("name") or "").strip()
    return str(getattr(ws, "name", None) or "").strip()


def _get_path(ws: Any) -> str:
    if isinstance(ws, dict):
        return str(ws.get("path") or "").strip()
    return str(getattr(ws, "path", None) or "").strip()


def _get_data(ws: Any) -> dict[str, Any]:
    if isinstance(ws, dict):
        raw = ws.get("data")
    else:
        raw = getattr(ws, "data", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _get_explicit_slugs(ws: Any) -> set[str]:
    slugs: set[str] = set()
    if isinstance(ws, dict):
        val = ws.get("slug")
        if isinstance(val, str) and val.strip():
            s = slugify(val)
            if s:
                slugs.add(s)
        vals = ws.get("slugs")
        if isinstance(vals, (list, tuple, set)):
            for item in vals:
                if isinstance(item, str) and item.strip():
                    s = slugify(item)
                    if s:
                        slugs.add(s)
    else:
        val = getattr(ws, "slug", None)
        if isinstance(val, str) and val.strip():
            s = slugify(val)
            if s:
                slugs.add(s)
        vals = getattr(ws, "slugs", None)
        if isinstance(vals, (list, tuple, set)):
            for item in vals:
                if isinstance(item, str) and item.strip():
                    s = slugify(item)
                    if s:
                        slugs.add(s)

    data = _get_data(ws)
    explicit_slug = data.get("slug")
    if isinstance(explicit_slug, str) and explicit_slug.strip():
        s = slugify(explicit_slug)
        if s:
            slugs.add(s)
    data_slugs = data.get("slugs")
    if isinstance(data_slugs, (list, tuple, set)):
        for item in data_slugs:
            if isinstance(item, str) and item.strip():
                s = slugify(item)
                if s:
                    slugs.add(s)

    return slugs


def _get_derived_slug(ws: Any) -> str:
    return slugify(_get_name(ws))


def _get_slugs(ws: Any) -> set[str]:
    slugs = set(_get_explicit_slugs(ws))
    name_slug = _get_derived_slug(ws)
    if name_slug:
        slugs.add(name_slug)
    return slugs


def _get_aliases(ws: Any) -> set[str]:
    aliases: set[str] = set()

    if isinstance(ws, dict):
        alias_val = ws.get("alias")
        if isinstance(alias_val, str) and alias_val.strip():
            aliases.add(alias_val.strip().lower())
        aliases_val = ws.get("aliases")
        if isinstance(aliases_val, (list, tuple, set)):
            for item in aliases_val:
                if isinstance(item, str) and item.strip():
                    aliases.add(item.strip().lower())
    else:
        alias_val = getattr(ws, "alias", None)
        if isinstance(alias_val, str) and alias_val.strip():
            aliases.add(alias_val.strip().lower())
        aliases_val = getattr(ws, "aliases", None)
        if isinstance(aliases_val, (list, tuple, set)):
            for item in aliases_val:
                if isinstance(item, str) and item.strip():
                    aliases.add(item.strip().lower())

    data = _get_data(ws)
    alias_val = data.get("alias")
    if isinstance(alias_val, str) and alias_val.strip():
        aliases.add(alias_val.strip().lower())

    aliases_val = data.get("aliases")
    if isinstance(aliases_val, (list, tuple, set)):
        for item in aliases_val:
            if isinstance(item, str) and item.strip():
                aliases.add(item.strip().lower())
    elif isinstance(aliases_val, dict):
        for item in aliases_val.values():
            if isinstance(item, str) and item.strip():
                aliases.add(item.strip().lower())

    display_alias = data.get("display_alias")
    if isinstance(display_alias, str) and display_alias.strip():
        aliases.add(display_alias.strip().lower())

    return aliases


def _paths_match(candidate_path: str, query_path: str) -> bool:
    if not candidate_path or not query_path:
        return False

    clean_query = query_path.strip().strip("'").strip('"')
    if clean_query.startswith("file://"):
        clean_query = clean_query[7:]

    cand_stripped = candidate_path.strip().rstrip("/" + chr(92))
    query_stripped = clean_query.rstrip("/" + chr(92))
    if candidate_path == clean_query or cand_stripped == query_stripped:
        return True

    # URL-decoded match (handling %20 and escaped characters in paths)
    unquoted_query = urllib.parse.unquote(clean_query).rstrip("/" + chr(92))
    if cand_stripped == unquoted_query:
        return True

    try:
        cand_resolved = Path(candidate_path).expanduser().resolve()
        query_resolved = Path(clean_query).expanduser().resolve()
        if cand_resolved == query_resolved:
            return True
    except Exception:
        pass

    try:
        cand_resolved = Path(candidate_path).expanduser().resolve()
        unquoted_resolved = Path(unquoted_query).expanduser().resolve()
        if cand_resolved == unquoted_resolved:
            return True
    except Exception:
        pass

    return False


def _find_fuzzy_matches(workspaces: Sequence[Any], query: str) -> list[Any]:
    query_lower = query.lower()
    query_slug = slugify(query)

    # Priority 1: Substring in workspace name or repo directory basename
    name_repo_matches: list[Any] = []
    for ws in workspaces:
        name_lower = _get_name(ws).lower()
        repo_lower = Path(_get_path(ws)).name.lower()
        if (query_lower and query_lower in name_lower) or (
            query_lower and query_lower in repo_lower
        ):
            name_repo_matches.append(ws)
        elif query_slug and (
            query_slug in slugify(name_lower) or query_slug in slugify(repo_lower)
        ):
            name_repo_matches.append(ws)

    if name_repo_matches:
        return name_repo_matches

    # Priority 2: Substring anywhere in path
    path_matches: list[Any] = []
    for ws in workspaces:
        path_lower = _get_path(ws).lower()
        if query_lower and query_lower in path_lower:
            path_matches.append(ws)

    return path_matches


class WorkspaceResolver:
    """Resolves workspace queries according to deterministic precedence and ambiguity rules."""

    def __init__(self, default_workspaces: Sequence[Any] | None = None) -> None:
        self._default_workspaces = default_workspaces

    def resolve_detailed(
        self,
        query: str,
        *,
        workspaces: Sequence[Any] | None = None,
        aliases: Sequence[Any] | None = None,
        destructive: bool = False,
        allow_fuzzy: bool = True,
    ) -> ResolutionResult:
        """Resolve a query to a single workspace with full match provenance.

        Precedence hierarchy:
            1. Exact Workspace ID / UUID match
            2. Unique workspace explicit slug match (WorkspaceRef compatibility)
            3. Unique workspace alias match
            4. Legacy exact filesystem path match
            5. Exact workspace name match (case-insensitive)
            6. Derived slug match (slugified name fallback)
            7. Repository basename match (directory name from path)
            8. Cross-repo intent / fuzzy match
        """
        raw_query = (query or "").strip()
        if not raw_query:
            raise ValueError("workspace query must not be blank")

        pool = list(workspaces if workspaces is not None else (self._default_workspaces or []))

        # 1. Exact Workspace ID / UUID Match
        uuid_matches = [ws for ws in pool if _get_id(ws).lower() == raw_query.lower()]
        if len(uuid_matches) == 1:
            return ResolutionResult(
                workspace=uuid_matches[0],
                stage=ResolutionStage.UUID,
                query=raw_query,
                confidence=1.0,
            )
        if len(uuid_matches) > 1:
            raise AmbiguousWorkspaceError(
                f"Ambiguous workspace query '{raw_query}': multiple workspaces matched UUID",
                query=raw_query,
                candidates=uuid_matches,
                stage=ResolutionStage.UUID,
            )

        # 2. Unique Workspace Slug Match (explicit slug)
        query_slug = slugify(raw_query)
        if query_slug:
            explicit_slug_matches = [ws for ws in pool if query_slug in _get_explicit_slugs(ws)]
            if len(explicit_slug_matches) == 1:
                return ResolutionResult(
                    workspace=explicit_slug_matches[0],
                    stage=ResolutionStage.SLUG,
                    query=raw_query,
                    confidence=0.95,
                )
            if len(explicit_slug_matches) > 1:
                raise AmbiguousWorkspaceError(
                    f"Ambiguous workspace query '{raw_query}': multiple workspaces matched slug '{query_slug}'",
                    query=raw_query,
                    candidates=explicit_slug_matches,
                    stage=ResolutionStage.SLUG,
                )

        # 3. Unique Workspace Alias Match
        query_alias = raw_query.lower()
        alias_matched_ws_ids: set[str] = set()
        if aliases:
            for al in aliases:
                al_name = getattr(al, "alias", None) or (
                    al.get("alias") if isinstance(al, dict) else None
                )
                if isinstance(al_name, str) and al_name.strip().lower() == query_alias:
                    ws_id = getattr(al, "workspace_id", None) or (
                        al.get("workspace_id") if isinstance(al, dict) else None
                    )
                    if ws_id:
                        alias_matched_ws_ids.add(str(ws_id).strip().lower())

        alias_matches = [
            ws
            for ws in pool
            if query_alias in _get_aliases(ws) or _get_id(ws).lower() in alias_matched_ws_ids
        ]
        if len(alias_matches) == 1:
            return ResolutionResult(
                workspace=alias_matches[0],
                stage=ResolutionStage.ALIAS,
                query=raw_query,
                confidence=0.90,
            )
        if len(alias_matches) > 1:
            raise AmbiguousWorkspaceError(
                f"Ambiguous workspace query '{raw_query}': multiple workspaces matched alias '{raw_query}'",
                query=raw_query,
                candidates=alias_matches,
                stage=ResolutionStage.ALIAS,
            )

        # 4. Legacy Exact Path Match
        path_matches = [ws for ws in pool if _paths_match(_get_path(ws), raw_query)]
        if len(path_matches) == 1:
            return ResolutionResult(
                workspace=path_matches[0],
                stage=ResolutionStage.EXACT_PATH,
                query=raw_query,
                confidence=1.0,
            )
        if len(path_matches) > 1:
            raise AmbiguousWorkspaceError(
                f"Ambiguous workspace query '{raw_query}': multiple workspaces matched exact path",
                query=raw_query,
                candidates=path_matches,
                stage=ResolutionStage.EXACT_PATH,
            )

        # 5. Exact Name Match (case-insensitive)
        name_matches = [ws for ws in pool if _get_name(ws).strip().lower() == raw_query.lower()]
        if len(name_matches) == 1:
            return ResolutionResult(
                workspace=name_matches[0],
                stage=ResolutionStage.EXACT_NAME,
                query=raw_query,
                confidence=0.95,
            )
        if len(name_matches) > 1:
            candidate_details = [
                f"{_get_name(ws)} ({_get_path(ws)} / {_get_id(ws)})" for ws in name_matches
            ]
            raise AmbiguousWorkspaceError(
                f"Ambiguous workspace query '{raw_query}': duplicate workspaces found with exact name '{raw_query}' "
                f"[{', '.join(candidate_details)}]",
                query=raw_query,
                candidates=name_matches,
                stage=ResolutionStage.EXACT_NAME,
            )

        # 6. Derived Slug Match (normalized name fallback when queried as slug)
        if query_slug:
            derived_slug_matches = [ws for ws in pool if query_slug == _get_derived_slug(ws)]
            if len(derived_slug_matches) == 1:
                return ResolutionResult(
                    workspace=derived_slug_matches[0],
                    stage=ResolutionStage.SLUG,
                    query=raw_query,
                    confidence=0.90,
                )
            if len(derived_slug_matches) > 1:
                raise AmbiguousWorkspaceError(
                    f"Ambiguous workspace query '{raw_query}': multiple workspaces matched slug '{query_slug}'",
                    query=raw_query,
                    candidates=derived_slug_matches,
                    stage=ResolutionStage.SLUG,
                )

        # 7. Repository Basename Match (directory name)
        repo_matches = [ws for ws in pool if Path(_get_path(ws)).name.lower() == raw_query.lower()]
        if len(repo_matches) == 1:
            return ResolutionResult(
                workspace=repo_matches[0],
                stage=ResolutionStage.REPO_MATCH,
                query=raw_query,
                confidence=0.85,
            )
        if len(repo_matches) > 1:
            raise AmbiguousWorkspaceError(
                f"Ambiguous workspace query '{raw_query}': multiple workspaces matched repository name '{raw_query}'",
                query=raw_query,
                candidates=repo_matches,
                stage=ResolutionStage.REPO_MATCH,
            )

        # 8. Cross-Repo Intent / Fuzzy Match
        fuzzy_matches = _find_fuzzy_matches(pool, raw_query)

        if destructive:
            if fuzzy_matches:
                raise UnsafeResolutionError(
                    f"Destructive operation rejected fuzzy match for '{raw_query}'. "
                    f"An exact identifier (UUID, path, unique slug, alias, or unique name) is required.",
                    query=raw_query,
                    candidates=fuzzy_matches,
                )
            raise WorkspaceNotFoundError(
                f"Workspace not found for '{raw_query}' (destructive operations require an exact identifier)",
                query=raw_query,
            )

        if not allow_fuzzy:
            raise WorkspaceNotFoundError(
                f"Workspace not found for '{raw_query}' (fuzzy resolution is disabled)",
                query=raw_query,
            )

        if len(fuzzy_matches) == 1:
            return ResolutionResult(
                workspace=fuzzy_matches[0],
                stage=ResolutionStage.FUZZY,
                query=raw_query,
                confidence=0.60,
            )
        if len(fuzzy_matches) > 1:
            raise AmbiguousWorkspaceError(
                f"Ambiguous workspace query '{raw_query}': multiple fuzzy matches found",
                query=raw_query,
                candidates=fuzzy_matches,
                stage=ResolutionStage.FUZZY,
            )

        raise WorkspaceNotFoundError(
            f"No workspace found matching query '{raw_query}'",
            query=raw_query,
        )

    def resolve(
        self,
        query: str,
        *,
        workspaces: Sequence[Any] | None = None,
        aliases: Sequence[Any] | None = None,
        destructive: bool = False,
        allow_fuzzy: bool = True,
    ) -> Any:
        result = self.resolve_detailed(
            query,
            workspaces=workspaces,
            aliases=aliases,
            destructive=destructive,
            allow_fuzzy=allow_fuzzy,
        )
        return result.workspace

    async def resolve_for_user(
        self,
        user_id: str,
        query: str,
        *,
        aliases: Sequence[Any] | None = None,
        destructive: bool = False,
        allow_fuzzy: bool = True,
        include_archived: bool = False,
    ) -> Any:
        if not (user_id or "").strip():
            raise ValueError("user_id must not be blank")
        from cptr.models.workspaces import Workspace

        workspaces = await Workspace.get_by_user(user_id)
        if not include_archived:
            workspaces = [ws for ws in workspaces if not bool(_get_data(ws).get("_cptr_archived"))]
        return self.resolve(
            query,
            workspaces=workspaces,
            aliases=aliases,
            destructive=destructive,
            allow_fuzzy=allow_fuzzy,
        )


def resolve_workspace(
    query: str,
    workspaces: Sequence[Any],
    *,
    aliases: Sequence[Any] | None = None,
    destructive: bool = False,
    allow_fuzzy: bool = True,
) -> Any:
    return WorkspaceResolver().resolve(
        query,
        workspaces=workspaces,
        aliases=aliases,
        destructive=destructive,
        allow_fuzzy=allow_fuzzy,
    )


async def resolve_workspace_for_user(
    user_id: str,
    query: str,
    *,
    aliases: Sequence[Any] | None = None,
    destructive: bool = False,
    allow_fuzzy: bool = True,
    include_archived: bool = False,
) -> Any:
    return await WorkspaceResolver().resolve_for_user(
        user_id=user_id,
        query=query,
        aliases=aliases,
        destructive=destructive,
        allow_fuzzy=allow_fuzzy,
        include_archived=include_archived,
    )
