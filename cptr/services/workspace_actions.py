"""Authoritative Workspace OS action orchestration for Control/MCP clients.

This module intentionally contains no second implementation of workspace
resolution, context compilation, health classification, or group semantics.
It only composes the canonical Workspace OS services behind a compact action
surface.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from cptr.memory.domain import PrepareContextInput
from cptr.memory.service import MemoryUnavailableError, get_memory_service
from cptr.memory.workspace import resolve_workspace_namespace
from cptr.models.environment_profile import EnvironmentProfileVersion
from cptr.models.workspaces import (
    Repository,
    RepositoryCheckout,
    Workspace,
    WorkspaceAlias,
    WorkspaceRepository,
)
from cptr.services.environment_profile import (
    EnvironmentProfileError,
    EnvironmentProfileNotFoundError,
    EnvironmentProfileService,
)
from cptr.services.environment_target import EnvironmentTargetError
from cptr.services.workspace_availability import is_workspace_available
from cptr.services.workspace_context import workspace_context_service
from cptr.services.workspace_groups import (
    WorkspaceGroupDuplicateMemberError,
    WorkspaceGroupError,
    WorkspaceGroupNotFoundError,
    WorkspaceGroupService,
    WorkspaceGroupSlugConflictError,
    WorkspaceGroupValidationError,
    WorkspaceGroupWorkspaceNotFoundError,
)
from cptr.services.workspace_health import (
    classify_workspace_health,
    conservative_reconcile_workspace,
)
from cptr.services.workspace_instructions import (
    WorkspaceInstructionError,
    WorkspaceInstructionService,
)
from cptr.services.workspace_resolver import (
    AmbiguousWorkspaceError,
    UnsafeResolutionError,
    WorkspaceNotFoundError,
    WorkspaceResolver,
)
from cptr.services.workbench_sessions import workbench_session_store
from cptr.services.workspace_tasks import WorkspaceTaskError, workspace_task_service
from cptr.utils.db import get_db
from cptr.utils.identity import identity_for_user_id
from cptr.utils.redaction import redact_sensitive


READ_ACTIONS = frozenset(
    {
        "resolve",
        "context",
        "health",
        "groups",
        "repositories",
        "repository_catalog",
        "instructions",
        "instruction_history",
        "instruction_preview",
        "environment_profiles",
        "environment_versions",
        "checkpoints",
        "tasks",
        "task_summary",
    }
)
WRITE_ACTIONS = frozenset(
    {
        "reconcile",
        "group_create",
        "group_update",
        "group_add_member",
        "group_remove_member",
        "group_update_member",
        "group_reorder",
        "instruction_save",
        "environment_create",
        "environment_version_create",
        "environment_set_active_version",
        "environment_set_target",
        "task_create",
        "task_update_status",
        "task_pin_repository",
        "task_add_evidence",
        "workspace_update",
        "repository_add",
        "repository_update",
        "repository_remove",
    }
)
SUPPORTED_ACTIONS = READ_ACTIONS | WRITE_ACTIONS


class WorkspaceActionError(RuntimeError):
    """Stable error contract for the compact Workspace OS action surface."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = int(status_code)
        self.details = dict(details or {})


def _workspace_summary(workspace: Workspace) -> dict[str, Any]:
    return {
        "workspace_id": str(workspace.id),
        "name": str(workspace.name),
        "slug": str(workspace.slug) if workspace.slug else None,
        "workspace_type": str(workspace.workspace_type or "project"),
        "available": is_workspace_available(workspace),
        "last_used_at": workspace.updated_at or workspace.created_at,
    }


def _map_group_error(exc: WorkspaceGroupError) -> WorkspaceActionError:
    if isinstance(exc, (WorkspaceGroupNotFoundError, WorkspaceGroupWorkspaceNotFoundError)):
        status_code = 404
    elif isinstance(exc, (WorkspaceGroupDuplicateMemberError, WorkspaceGroupSlugConflictError)):
        status_code = 409
    elif isinstance(exc, WorkspaceGroupValidationError):
        status_code = 422
    else:
        status_code = 400
    return WorkspaceActionError(
        str(getattr(exc, "code", "WORKSPACE_GROUP_ERROR")),
        str(exc),
        status_code=status_code,
    )


def _safe_environment_version(version: Any) -> dict[str, Any] | None:
    if version is None:
        return None
    credential_refs = version.credential_refs if isinstance(version.credential_refs, list) else []
    packages = version.packages if isinstance(version.packages, (dict, list)) else {}
    settings = version.settings if isinstance(version.settings, dict) else {}
    env = version.environment_variables if isinstance(version.environment_variables, dict) else {}
    return {
        "version_id": str(version.id),
        "version_number": int(version.version_number),
        "digest": str(version.digest),
        "runtime_profile": str(version.runtime_profile),
        "environment_variable_names": sorted(str(key) for key in env),
        "package_count": len(packages),
        "setting_keys": sorted(str(key) for key in settings),
        "credential_refs": [
            {
                "logical_name": ref.get("logical_name"),
                "source_type": ref.get("source_type"),
                "target_env_var": ref.get("target_env_var"),
                "consumers": list(ref.get("consumers") or []),
            }
            for ref in credential_refs
            if isinstance(ref, dict)
        ],
        "parent_version_id": (
            str(version.parent_version_id) if version.parent_version_id else None
        ),
        "created_at_ms": int(version.created_at_ms),
    }


def _safe_environment_profile(profile: Any, version: Any = None) -> dict[str, Any]:
    return {
        "profile_id": str(profile.id),
        "workspace_id": str(profile.workspace_id) if profile.workspace_id else None,
        "name": str(profile.name),
        "description": str(profile.description) if profile.description else None,
        "active_version_id": (
            str(profile.active_version_id) if profile.active_version_id else None
        ),
        "target_name": str(profile.target_name) if profile.target_name else None,
        "is_archived": bool(profile.is_archived),
        "created_at_ms": int(profile.created_at_ms),
        "updated_at_ms": int(profile.updated_at_ms),
        "active_version": _safe_environment_version(version),
    }


def _candidate_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            "workspace_id": str(value.get("workspace_id") or value.get("id") or ""),
            "name": str(value.get("name") or ""),
            "slug": value.get("slug"),
        }
    return {
        "workspace_id": str(getattr(value, "id", "") or ""),
        "name": str(getattr(value, "name", "") or ""),
        "slug": getattr(value, "slug", None),
    }


class WorkspaceActionService:
    """Thin orchestration layer over authoritative Workspace OS services."""

    def __init__(self) -> None:
        self._resolver = WorkspaceResolver()
        self._instructions = WorkspaceInstructionService()

    async def _aliases(self, user_id: str) -> list[WorkspaceAlias]:
        async with await get_db() as db:
            rows = await db.scalars(select(WorkspaceAlias).where(WorkspaceAlias.user_id == user_id))
            return list(rows.all())

    async def _resolve(
        self,
        *,
        user_id: str,
        reference: str,
        destructive: bool = False,
        allow_fuzzy: bool = True,
        include_archived: bool = False,
    ) -> Workspace:
        ref = str(reference or "").strip()
        if not ref:
            raise WorkspaceActionError(
                "WORKSPACE_REF_REQUIRED",
                "workspace reference is required",
                status_code=422,
            )
        aliases = await self._aliases(user_id)
        try:
            return await self._resolver.resolve_for_user(
                user_id=user_id,
                query=ref,
                aliases=aliases,
                destructive=destructive,
                allow_fuzzy=allow_fuzzy,
                include_archived=include_archived,
            )
        except WorkspaceNotFoundError as exc:
            raise WorkspaceActionError(
                "WORKSPACE_NOT_FOUND",
                str(exc),
                status_code=404,
                details={"reference": ref},
            ) from exc
        except AmbiguousWorkspaceError as exc:
            raise WorkspaceActionError(
                "WORKSPACE_AMBIGUOUS",
                str(exc),
                status_code=409,
                details={
                    "reference": ref,
                    "stage": exc.stage,
                    "candidates": [_candidate_summary(value) for value in exc.candidates],
                },
            ) from exc
        except UnsafeResolutionError as exc:
            raise WorkspaceActionError(
                "WORKSPACE_UNSAFE_RESOLUTION",
                str(exc),
                status_code=422,
                details={
                    "reference": ref,
                    "candidates": [_candidate_summary(value) for value in exc.candidates],
                },
            ) from exc
        except ValueError as exc:
            raise WorkspaceActionError(
                "WORKSPACE_INVALID_REFERENCE",
                str(exc),
                status_code=422,
                details={"reference": ref},
            ) from exc

    async def resolve(
        self,
        *,
        user_id: str,
        reference: str,
        destructive: bool = False,
        allow_fuzzy: bool = True,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        aliases = await self._aliases(user_id)
        ref = str(reference or "").strip()
        if not ref:
            raise WorkspaceActionError(
                "WORKSPACE_REF_REQUIRED",
                "workspace reference is required",
                status_code=422,
            )
        try:
            result = await self._resolver.resolve_detailed_for_user(
                user_id=user_id,
                query=ref,
                aliases=aliases,
                destructive=destructive,
                allow_fuzzy=allow_fuzzy,
                include_archived=include_archived,
            )
        except WorkspaceNotFoundError as exc:
            raise WorkspaceActionError(
                "WORKSPACE_NOT_FOUND",
                str(exc),
                status_code=404,
                details={"reference": ref},
            ) from exc
        except AmbiguousWorkspaceError as exc:
            raise WorkspaceActionError(
                "WORKSPACE_AMBIGUOUS",
                str(exc),
                status_code=409,
                details={
                    "reference": ref,
                    "stage": exc.stage,
                    "candidates": [_candidate_summary(value) for value in exc.candidates],
                },
            ) from exc
        except UnsafeResolutionError as exc:
            raise WorkspaceActionError(
                "WORKSPACE_UNSAFE_RESOLUTION",
                str(exc),
                status_code=422,
                details={
                    "reference": ref,
                    "candidates": [_candidate_summary(value) for value in exc.candidates],
                },
            ) from exc

        return {
            "workspace": _workspace_summary(result.workspace),
            "matched_by": result.stage.value,
            "confidence": float(result.confidence),
            "metadata": redact_sensitive(dict(result.metadata)),
        }

    async def _workbench(
        self,
        *,
        user_id: str,
        workbench_session_id: str | None,
    ) -> dict[str, Any] | None:
        if not workbench_session_id:
            return None
        session = await workbench_session_store.get(
            owner_id=user_id,
            session_id=workbench_session_id,
        )
        if session is None:
            raise WorkspaceActionError(
                "WORKBENCH_NOT_FOUND",
                f"Workbench session not found: {workbench_session_id}",
                status_code=404,
            )
        return session

    async def _context_reference(
        self,
        *,
        user_id: str,
        reference: str | None,
        workspace_id: str | None,
        workbench: dict[str, Any] | None,
    ) -> str:
        explicit = str(reference or workspace_id or "").strip()
        bound = ""
        if workbench:
            bound = str(
                workbench.get("active_workspace_id") or workbench.get("workspace_id") or ""
            ).strip()
        if explicit:
            if bound:
                explicit_ws = await self._resolve(
                    user_id=user_id,
                    reference=explicit,
                    allow_fuzzy=False,
                )
                bound_ws = await self._resolve(
                    user_id=user_id,
                    reference=bound,
                    allow_fuzzy=False,
                )
                if str(explicit_ws.id) != str(bound_ws.id):
                    raise WorkspaceActionError(
                        "WORKSPACE_WORKBENCH_MISMATCH",
                        "explicit workspace does not match the Workbench-bound workspace",
                        status_code=409,
                        details={
                            "workspace_id": str(explicit_ws.id),
                            "workbench_workspace_id": str(bound_ws.id),
                        },
                    )
            return explicit
        if bound:
            return bound
        raise WorkspaceActionError(
            "WORKSPACE_REF_REQUIRED",
            "workspace_id/reference or a bound Workbench session is required",
            status_code=422,
        )

    async def _memory_input(
        self,
        *,
        user_id: str,
        workspace: Workspace,
        current_message: str,
        max_chars: int,
        task_key: str = "",
        recent_messages: list[dict[str, Any]] | None = None,
        mentioned_files: list[str] | None = None,
    ) -> tuple[Any | None, list[str]]:
        diagnostics: list[str] = []
        namespace = await resolve_workspace_namespace(user_id, str(workspace.id))
        try:
            bundle = await get_memory_service().prepare_context(
                PrepareContextInput(
                    user_id=user_id,
                    workspace=namespace.workspace_id or str(workspace.id),
                    task_key=str(task_key or ""),
                    current_message=current_message,
                    recent_messages=recent_messages or [],
                    mentioned_files=mentioned_files or [],
                    max_chars=max_chars,
                )
            )
            return bundle, diagnostics
        except MemoryUnavailableError as exc:
            raise WorkspaceActionError(
                "WORKSPACE_MEMORY_UNAVAILABLE",
                str(exc),
                status_code=503,
                details={"workspace_id": str(workspace.id)},
            ) from exc
        except Exception as exc:
            diagnostics.append(f"workspace memory degraded: {exc}")
            return {
                "enabled": False,
                "status": "degraded",
                "error": f"workspace memory degraded: {exc}",
            }, diagnostics

    async def _environment_input(
        self,
        *,
        user_id: str,
        workspace: Workspace,
        workbench: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
        diagnostics: list[str] = []
        profile_id = str(workbench.get("environment_profile_id") or "").strip() if workbench else ""
        override = (
            workbench.get("environment_profile_override")
            if workbench and isinstance(workbench.get("environment_profile_override"), dict)
            else {}
        )

        async with await get_db() as db:
            if not profile_id:
                profiles = await EnvironmentProfileService.list_profiles(
                    db,
                    user_id=user_id,
                    workspace_id=str(workspace.id),
                    include_archived=False,
                )
                if len(profiles) == 1:
                    profile_id = str(profiles[0].id)
                elif len(profiles) > 1:
                    diagnostics.append(
                        "multiple environment profiles are available; bind one to the Workbench"
                    )
                    return None, None, diagnostics
                else:
                    return None, None, diagnostics

            profile = await EnvironmentProfileService.get_profile(db, profile_id)
            if profile is None or str(profile.user_id) != str(user_id):
                raise WorkspaceActionError(
                    "ENVIRONMENT_PROFILE_NOT_FOUND",
                    f"environment profile not found: {profile_id}",
                    status_code=404,
                )
            if profile.workspace_id and str(profile.workspace_id) != str(workspace.id):
                raise WorkspaceActionError(
                    "ENVIRONMENT_WORKSPACE_MISMATCH",
                    "Workbench environment profile belongs to a different workspace",
                    status_code=409,
                    details={
                        "workspace_id": str(workspace.id),
                        "environment_profile_id": profile_id,
                    },
                )

            version = (
                await EnvironmentProfileService.get_version(db, profile.active_version_id)
                if profile.active_version_id
                else None
            )
            target = None
            target_name_override = str(override.get("target_name") or "").strip() or None
            if version is not None:
                try:
                    target = await EnvironmentProfileService.resolve_profile_target(
                        db,
                        profile_id=profile_id,
                        caller_user_id=user_id,
                        target_name_override=target_name_override,
                    )
                except Exception as exc:
                    diagnostics.append(f"environment target degraded: {exc}")

            safe_profile = {
                "profile_id": str(profile.id),
                "name": str(profile.name),
                "target_name": str(profile.target_name) if profile.target_name else None,
                "active_version_id": str(profile.active_version_id)
                if profile.active_version_id
                else None,
                "version_number": int(version.version_number) if version is not None else None,
                "version_digest": str(version.digest) if version is not None else None,
                "runtime_profile": str(version.runtime_profile) if version is not None else None,
                "target": target.to_dict() if target is not None else None,
            }
            environment_input = {"extra": safe_profile}
            return environment_input, safe_profile, diagnostics

    async def _invalidate_axis(
        self,
        *,
        user_id: str,
        workspace_id: str,
        axis: str,
    ) -> None:
        """Invalidate cached context and publish the matching Workspace OS event."""
        from cptr.events import EVENTS
        from cptr.services.live_events import safe_publish_workspace_event
        from cptr.services.workspace_observability import workspace_context_cache

        event_names = {
            "instructions": EVENTS.WORKSPACE_INSTRUCTIONS_CHANGED.name,
            "environment": EVENTS.WORKSPACE_ENVIRONMENT_CHANGED.name,
            "memory": EVENTS.WORKSPACE_MEMORY_CHANGED.name,
            "checkpoint": EVENTS.WORKSPACE_CHECKPOINT_CHANGED.name,
            "revision": EVENTS.WORKSPACE_REVISION_CHANGED.name,
            "role": EVENTS.WORKSPACE_ROLE_CHANGED.name,
            "canonical_checkout": EVENTS.WORKSPACE_CHECKOUT_CHANGED.name,
            "task": EVENTS.WORKSPACE_TASK_CHANGED.name,
        }
        await workspace_context_cache.invalidate(
            workspace_id,
            reason=axis,
            user_id=user_id,
        )
        event_name = event_names.get(axis)
        if event_name:
            await safe_publish_workspace_event(
                user_id=user_id,
                workspace_id=workspace_id,
                event_type=event_name,
                payload={"workspace_id": workspace_id, "axis": axis},
            )

    async def workspace_update(
        self,
        *,
        user_id: str,
        reference: str,
        name: str | None = None,
        slug: str | None = None,
        workspace_type: str | None = None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        try:
            updated = await Workspace.update_identity(
                user_id,
                str(workspace.id),
                name=name,
                slug=slug,
                workspace_type=workspace_type,
            )
        except ValueError as exc:
            code = (
                "WORKSPACE_SLUG_CONFLICT"
                if "slug is already in use" in str(exc)
                else "WORKSPACE_IDENTITY_INVALID"
            )
            raise WorkspaceActionError(
                code,
                str(exc),
                status_code=409 if code == "WORKSPACE_SLUG_CONFLICT" else 422,
            ) from exc
        return {"workspace": _workspace_summary(updated)}

    async def repository_catalog(
        self,
        *,
        user_id: str,
    ) -> dict[str, Any]:
        async with await get_db() as db:
            repositories = list(
                (
                    await db.scalars(
                        select(Repository)
                        .where(Repository.user_id == user_id)
                        .order_by(Repository.name, Repository.canonical_remote_identity)
                    )
                ).all()
            )
        return {
            "repositories": [
                {
                    "repository_id": str(repository.id),
                    "name": str(repository.name),
                    "canonical_remote_identity": str(repository.canonical_remote_identity),
                    "provider": str(repository.provider) if repository.provider else None,
                    "default_branch": (
                        str(repository.default_branch) if repository.default_branch else None
                    ),
                }
                for repository in repositories
            ]
        }

    async def _workspace_membership(
        self,
        *,
        db: Any,
        user_id: str,
        workspace_id: str,
        repository_id: str,
    ) -> tuple[Repository, WorkspaceRepository | None]:
        repository = await db.scalar(
            select(Repository).where(
                Repository.id == repository_id,
                Repository.user_id == user_id,
            )
        )
        if repository is None:
            raise WorkspaceActionError(
                "REPOSITORY_NOT_FOUND",
                f"repository not found: {repository_id}",
                status_code=404,
            )
        membership = await db.get(
            WorkspaceRepository,
            {"workspace_id": workspace_id, "repository_id": repository_id},
        )
        return repository, membership

    async def repository_add(
        self,
        *,
        user_id: str,
        reference: str,
        repository_id: str,
        role: str = "repository",
        primary: bool = False,
        sort_order: int = 0,
        enabled: bool = True,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        workspace_id = str(workspace.id)
        async with await get_db() as db:
            repository, membership = await self._workspace_membership(
                db=db,
                user_id=user_id,
                workspace_id=workspace_id,
                repository_id=repository_id,
            )
            if membership is not None:
                raise WorkspaceActionError(
                    "REPOSITORY_ALREADY_IN_WORKSPACE",
                    f"repository is already in workspace: {repository_id}",
                    status_code=409,
                )
            if primary:
                existing = list(
                    (
                        await db.scalars(
                            select(WorkspaceRepository).where(
                                WorkspaceRepository.workspace_id == workspace_id,
                                WorkspaceRepository.primary.is_(True),
                            )
                        )
                    ).all()
                )
                for row in existing:
                    row.primary = False
            membership = WorkspaceRepository(
                workspace_id=workspace_id,
                repository_id=str(repository.id),
                role=str(role or "repository").strip() or "repository",
                primary=bool(primary),
                sort_order=int(sort_order),
                enabled=bool(enabled),
                config=redact_sensitive(dict(config or {})),
            )
            db.add(membership)
            await db.commit()
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=workspace_id,
            axis="canonical_checkout",
        )
        return await self.repositories(user_id=user_id, reference=workspace_id)

    async def repository_update(
        self,
        *,
        user_id: str,
        reference: str,
        repository_id: str,
        role: str | None = None,
        primary: bool | None = None,
        sort_order: int | None = None,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        workspace_id = str(workspace.id)
        async with await get_db() as db:
            _, membership = await self._workspace_membership(
                db=db,
                user_id=user_id,
                workspace_id=workspace_id,
                repository_id=repository_id,
            )
            if membership is None:
                raise WorkspaceActionError(
                    "WORKSPACE_REPOSITORY_NOT_FOUND",
                    "repository membership not found",
                    status_code=404,
                )
            if primary is True:
                existing = list(
                    (
                        await db.scalars(
                            select(WorkspaceRepository).where(
                                WorkspaceRepository.workspace_id == workspace_id,
                                WorkspaceRepository.primary.is_(True),
                                WorkspaceRepository.repository_id != repository_id,
                            )
                        )
                    ).all()
                )
                for row in existing:
                    row.primary = False
            if role is not None:
                membership.role = str(role).strip() or "repository"
            if primary is not None:
                membership.primary = bool(primary)
            if sort_order is not None:
                membership.sort_order = int(sort_order)
            if enabled is not None:
                membership.enabled = bool(enabled)
            if config is not None:
                membership.config = redact_sensitive(dict(config))
            await db.commit()
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=workspace_id,
            axis="canonical_checkout",
        )
        return await self.repositories(user_id=user_id, reference=workspace_id)

    async def repository_remove(
        self,
        *,
        user_id: str,
        reference: str,
        repository_id: str,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        workspace_id = str(workspace.id)
        async with await get_db() as db:
            _, membership = await self._workspace_membership(
                db=db,
                user_id=user_id,
                workspace_id=workspace_id,
                repository_id=repository_id,
            )
            if membership is None:
                raise WorkspaceActionError(
                    "WORKSPACE_REPOSITORY_NOT_FOUND",
                    "repository membership not found",
                    status_code=404,
                )
            await db.delete(membership)
            await db.commit()
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=workspace_id,
            axis="canonical_checkout",
        )
        return await self.repositories(user_id=user_id, reference=workspace_id)

    async def repositories(
        self,
        *,
        user_id: str,
        reference: str,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        workspace_id = str(workspace.id)
        async with await get_db() as db:
            rows = list(
                (
                    await db.execute(
                        select(WorkspaceRepository, Repository)
                        .join(
                            Repository,
                            Repository.id == WorkspaceRepository.repository_id,
                        )
                        .where(
                            WorkspaceRepository.workspace_id == workspace_id,
                            Repository.user_id == user_id,
                        )
                        .order_by(
                            WorkspaceRepository.sort_order,
                            Repository.name,
                        )
                    )
                ).all()
            )
            repo_ids = [str(repository.id) for _, repository in rows]
            checkouts_by_repo: dict[str, list[Any]] = {repo_id: [] for repo_id in repo_ids}
            if repo_ids:
                checkout_rows = list(
                    (
                        await db.scalars(
                            select(RepositoryCheckout)
                            .where(RepositoryCheckout.repository_id.in_(repo_ids))
                            .order_by(
                                RepositoryCheckout.repository_id,
                                RepositoryCheckout.canonical.desc(),
                                RepositoryCheckout.created_at,
                            )
                        )
                    ).all()
                )
                for checkout in checkout_rows:
                    checkouts_by_repo.setdefault(str(checkout.repository_id), []).append(checkout)

        repositories: list[dict[str, Any]] = []
        for membership, repository in rows:
            repository_id = str(repository.id)
            checkouts = checkouts_by_repo.get(repository_id, [])
            repositories.append(
                {
                    "repository_id": repository_id,
                    "name": str(repository.name),
                    "canonical_remote_identity": str(repository.canonical_remote_identity),
                    "provider": str(repository.provider) if repository.provider else None,
                    "default_branch": (
                        str(repository.default_branch) if repository.default_branch else None
                    ),
                    "role": str(membership.role),
                    "primary": bool(membership.primary),
                    "sort_order": int(membership.sort_order or 0),
                    "enabled": bool(membership.enabled),
                    "config": redact_sensitive(
                        membership.config if isinstance(membership.config, dict) else {}
                    ),
                    "checkouts": [
                        {
                            "checkout_id": str(checkout.id),
                            "checkout_kind": str(checkout.checkout_kind),
                            "canonical": bool(checkout.canonical),
                            "branch": str(checkout.branch) if checkout.branch else None,
                            "upstream": str(checkout.upstream) if checkout.upstream else None,
                            "last_seen_revision": (
                                str(checkout.last_seen_revision)
                                if checkout.last_seen_revision
                                else None
                            ),
                            "available": bool(checkout.available),
                            "last_seen_at": int(checkout.last_seen_at),
                        }
                        for checkout in checkouts
                    ],
                }
            )
        return {
            "workspace": _workspace_summary(workspace),
            "repositories": repositories,
            "repository_count": len(repositories),
        }

    async def instructions(
        self,
        *,
        user_id: str,
        reference: str,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        current = await self._instructions.get_current_instruction(
            user_id=user_id,
            workspace_id=str(workspace.id),
        )
        return {
            "workspace": _workspace_summary(workspace),
            "instruction": current.to_dict() if current is not None else None,
        }

    async def instruction_history(
        self,
        *,
        user_id: str,
        reference: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        rows, total = await self._instructions.get_instruction_history(
            user_id=user_id,
            workspace_id=str(workspace.id),
            limit=max(1, min(int(limit), 100)),
            offset=max(0, int(offset)),
        )
        return {
            "workspace": _workspace_summary(workspace),
            "instructions": [row.to_dict() for row in rows],
            "total": int(total),
        }

    async def instruction_preview(
        self,
        *,
        user_id: str,
        reference: str,
        candidate_content: str | None = None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        preview = await self._instructions.compile_preview(
            user_id=user_id,
            workspace_id=str(workspace.id),
            candidate_content=candidate_content,
        )
        return {
            "workspace": _workspace_summary(workspace),
            "preview": preview.to_dict(),
        }

    async def instruction_save(
        self,
        *,
        user_id: str,
        reference: str,
        content: str,
        expected_version: int | None = None,
        change_summary: str | None = None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        row = await self._instructions.save_instruction_version(
            user_id=user_id,
            workspace_id=str(workspace.id),
            content=str(content),
            expected_version=expected_version,
            change_summary=change_summary,
        )
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=str(workspace.id),
            axis="instructions",
        )
        return {
            "workspace": _workspace_summary(workspace),
            "instruction": row.to_dict(),
        }

    async def environment_profiles(
        self,
        *,
        user_id: str,
        reference: str,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        async with await get_db() as db:
            profiles = await EnvironmentProfileService.list_profiles(
                db,
                user_id=user_id,
                workspace_id=str(workspace.id),
                include_archived=bool(include_archived),
            )
            result: list[dict[str, Any]] = []
            for profile in profiles:
                version = (
                    await EnvironmentProfileService.get_version(db, str(profile.active_version_id))
                    if profile.active_version_id
                    else None
                )
                result.append(_safe_environment_profile(profile, version))
        return {
            "workspace": _workspace_summary(workspace),
            "profiles": result,
        }

    async def environment_versions(
        self,
        *,
        user_id: str,
        reference: str,
        profile_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        async with await get_db() as db:
            profile = await self._owned_environment_profile(
                db=db,
                user_id=user_id,
                workspace_id=str(workspace.id),
                profile_id=profile_id,
            )
            stmt = (
                select(EnvironmentProfileVersion)
                .where(EnvironmentProfileVersion.profile_id == str(profile.id))
                .order_by(EnvironmentProfileVersion.version_number.desc())
                .limit(max(1, min(int(limit), 200)))
            )
            versions = list((await db.scalars(stmt)).all())
            active = (
                await EnvironmentProfileService.get_version(db, str(profile.active_version_id))
                if profile.active_version_id
                else None
            )
            safe_profile = _safe_environment_profile(profile, active)
        return {
            "workspace": _workspace_summary(workspace),
            "profile": safe_profile,
            "versions": [_safe_environment_version(version) for version in versions],
        }

    async def _owned_environment_profile(
        self,
        *,
        db: Any,
        user_id: str,
        workspace_id: str,
        profile_id: str,
    ) -> Any:
        profile = await EnvironmentProfileService.get_profile(db, profile_id)
        if profile is None:
            raise WorkspaceActionError(
                "ENVIRONMENT_PROFILE_NOT_FOUND",
                f"environment profile not found: {profile_id}",
                status_code=404,
            )
        if str(profile.user_id) != str(user_id):
            raise WorkspaceActionError(
                "ENVIRONMENT_PROFILE_ACCESS_DENIED",
                "environment profile is not owned by the authenticated user",
                status_code=403,
            )
        if str(profile.workspace_id or "") != str(workspace_id):
            raise WorkspaceActionError(
                "ENVIRONMENT_WORKSPACE_MISMATCH",
                "environment profile belongs to a different workspace",
                status_code=409,
            )
        return profile

    async def environment_create(
        self,
        *,
        user_id: str,
        reference: str,
        name: str,
        description: str | None = None,
        initial_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        async with await get_db() as db:
            profile, version = await EnvironmentProfileService.create_profile(
                db,
                user_id=user_id,
                workspace_id=str(workspace.id),
                name=name,
                description=description,
                initial_spec=initial_spec,
            )
            await db.commit()
            await db.refresh(profile)
            if version is not None:
                await db.refresh(version)
            safe = _safe_environment_profile(profile, version)
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=str(workspace.id),
            axis="environment",
        )
        return {"workspace": _workspace_summary(workspace), "profile": safe}

    async def environment_version_create(
        self,
        *,
        user_id: str,
        reference: str,
        profile_id: str,
        spec: dict[str, Any],
        make_active: bool = True,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        async with await get_db() as db:
            profile = await self._owned_environment_profile(
                db=db,
                user_id=user_id,
                workspace_id=str(workspace.id),
                profile_id=profile_id,
            )
            version = await EnvironmentProfileService.create_version(
                db,
                profile_id=str(profile.id),
                spec=spec,
                created_by=user_id,
                make_active=bool(make_active),
            )
            await db.commit()
            await db.refresh(profile)
            await db.refresh(version)
            safe = _safe_environment_profile(profile, version if make_active else None)
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=str(workspace.id),
            axis="environment",
        )
        return {
            "workspace": _workspace_summary(workspace),
            "profile": safe,
            "version": _safe_environment_version(version),
        }

    async def environment_set_active_version(
        self,
        *,
        user_id: str,
        reference: str,
        profile_id: str,
        version_id: str,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        async with await get_db() as db:
            profile = await self._owned_environment_profile(
                db=db,
                user_id=user_id,
                workspace_id=str(workspace.id),
                profile_id=profile_id,
            )
            profile = await EnvironmentProfileService.set_active_version(
                db,
                profile_id=str(profile.id),
                version_id=version_id,
            )
            await db.commit()
            version = await EnvironmentProfileService.get_version(
                db, str(profile.active_version_id)
            )
            safe = _safe_environment_profile(profile, version)
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=str(workspace.id),
            axis="environment",
        )
        return {"workspace": _workspace_summary(workspace), "profile": safe}

    async def environment_set_target(
        self,
        *,
        user_id: str,
        reference: str,
        profile_id: str,
        target_name: str | None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        async with await get_db() as db:
            profile = await self._owned_environment_profile(
                db=db,
                user_id=user_id,
                workspace_id=str(workspace.id),
                profile_id=profile_id,
            )
            profile = await EnvironmentProfileService.set_profile_target(
                db,
                profile_id=str(profile.id),
                target_name=target_name,
                user_id=user_id,
            )
            await db.commit()
            version = (
                await EnvironmentProfileService.get_version(db, str(profile.active_version_id))
                if profile.active_version_id
                else None
            )
            safe = _safe_environment_profile(profile, version)
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=str(workspace.id),
            axis="environment",
        )
        return {"workspace": _workspace_summary(workspace), "profile": safe}

    async def tasks(
        self,
        *,
        user_id: str,
        reference: str,
        status: str | None = None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        rows = await workspace_task_service.list_tasks(
            user_id=user_id,
            workspace_id=str(workspace.id),
            status=status,
        )
        return {"workspace": _workspace_summary(workspace), "tasks": rows}

    async def task_summary(
        self,
        *,
        user_id: str,
        reference: str,
        task_id: str,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        summary = await workspace_task_service.summary(
            user_id=user_id,
            workspace=workspace,
            task_id=task_id,
        )
        return {"workspace": _workspace_summary(workspace), "summary": summary}

    async def task_create(
        self,
        *,
        user_id: str,
        reference: str,
        title: str,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        task = await workspace_task_service.create_task(
            user_id=user_id,
            workspace_id=str(workspace.id),
            title=title,
            description=description,
            metadata=metadata,
        )
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=str(workspace.id),
            axis="task",
        )
        return {"workspace": _workspace_summary(workspace), "task": task}

    async def task_update_status(
        self,
        *,
        user_id: str,
        reference: str,
        task_id: str,
        status: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        task = await workspace_task_service.update_task_status(
            user_id=user_id,
            workspace_id=str(workspace.id),
            task_id=task_id,
            status=status,
            description=description,
            metadata=metadata,
        )
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=str(workspace.id),
            axis="task",
        )
        return {"workspace": _workspace_summary(workspace), "task": task}

    async def task_pin_repository(
        self,
        *,
        user_id: str,
        reference: str,
        task_id: str,
        repo_path: str = ".",
        pinned_revision: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        pin = await workspace_task_service.pin_repository(
            user_id=user_id,
            workspace=workspace,
            task_id=task_id,
            repo_path=repo_path,
            pinned_revision=pinned_revision,
            branch=branch,
        )
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=str(workspace.id),
            axis="task",
        )
        return {"workspace": _workspace_summary(workspace), "pin": pin}

    async def task_add_evidence(
        self,
        *,
        user_id: str,
        reference: str,
        task_id: str,
        kind: str,
        status: str,
        summary: str,
        worker_id: str | None = None,
        repo_path: str | None = None,
        command: str | None = None,
        details: dict[str, Any] | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        evidence = await workspace_task_service.add_evidence(
            user_id=user_id,
            workspace_id=str(workspace.id),
            task_id=task_id,
            kind=kind,
            status=status,
            summary=summary,
            worker_id=worker_id,
            repo_path=repo_path,
            command=command,
            details=details,
            fingerprint=fingerprint,
        )
        await self._invalidate_axis(
            user_id=user_id,
            workspace_id=str(workspace.id),
            axis="task",
        )
        return {"workspace": _workspace_summary(workspace), "evidence": evidence}

    async def checkpoints(
        self,
        *,
        user_id: str,
        reference: str,
        task_key: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=False,
        )
        store = get_memory_service().store
        checkpoints = await store.list_checkpoints(
            user_id,
            str(workspace.id),
            task_key=task_key,
            limit=max(1, min(int(limit), 200)),
        )
        memory_version = await store.namespace_version(user_id, str(workspace.id))
        return {
            "workspace": _workspace_summary(workspace),
            "memory_version": int(memory_version),
            "checkpoints": checkpoints,
        }

    async def _context_impl(
        self,
        *,
        user_id: str,
        workspace_id: str | None = None,
        reference: str | None = None,
        workbench_session_id: str | None = None,
        current_message: str = "",
        max_chars: int = 9000,
        memory_max_chars: int = 3000,
        memory_task_key: str = "",
        recent_messages: list[dict[str, Any]] | None = None,
        mentioned_files: list[str] | None = None,
    ) -> dict[str, Any]:
        workbench = await self._workbench(
            user_id=user_id,
            workbench_session_id=workbench_session_id,
        )
        target_ref = await self._context_reference(
            user_id=user_id,
            reference=reference,
            workspace_id=workspace_id,
            workbench=workbench,
        )
        workspace = await self._resolve(
            user_id=user_id,
            reference=target_ref,
            allow_fuzzy=False,
        )

        instruction = await self._instructions.get_current_instruction(
            user_id=user_id,
            workspace_id=str(workspace.id),
        )
        instruction_input = None
        instruction_version = None
        if instruction is not None:
            instruction_version = int(instruction.version)
            instruction_input = {
                "user_instructions": (
                    f"[Workspace Instructions v{instruction.version}]\n{instruction.content}"
                )
            }

        memory_input, diagnostics = await self._memory_input(
            user_id=user_id,
            workspace=workspace,
            current_message=str(current_message or ""),
            max_chars=max(500, min(int(memory_max_chars), 6000)),
            task_key=memory_task_key,
            recent_messages=recent_messages,
            mentioned_files=mentioned_files,
        )
        environment_input, environment_profile, env_diagnostics = await self._environment_input(
            user_id=user_id,
            workspace=workspace,
            workbench=workbench,
        )
        diagnostics.extend(env_diagnostics)

        snapshot = await workspace_context_service.capture_snapshot(
            workspace_root=str(workspace.path),
            user_id=user_id,
            workspace_id=str(workspace.id),
            active_workspace_id=str(workspace.id),
            workbench_session=workbench,
            checkpoint=None,
            memory_input=memory_input,
            environment_input=environment_input,
            instruction_input=instruction_input,
        )
        if diagnostics:
            snapshot.diagnostics.extend(diagnostics)

        safe_max_chars = max(1000, min(int(max_chars), 12000))
        bundle = workspace_context_service.compile_bundle(
            snapshot,
            max_chars=safe_max_chars,
        )
        if workbench_session_id:
            await workbench_session_store.update(
                owner_id=user_id,
                session_id=workbench_session_id,
                last_context_snapshot_id=snapshot.snapshot_id,
            )

        return redact_sensitive(
            {
                "workspace": _workspace_summary(workspace),
                "instruction_version": instruction_version,
                "memory_version": snapshot.memory.memory_version,
                "environment_profile": environment_profile,
                "context": bundle,
            }
        )

    async def context(
        self,
        *,
        user_id: str,
        workspace_id: str | None = None,
        reference: str | None = None,
        workbench_session_id: str | None = None,
        current_message: str = "",
        max_chars: int = 9000,
        memory_max_chars: int = 3000,
    ) -> dict[str, Any]:
        """Compile read-only Workspace context without message-scoped checkpoint side effects."""
        return await self._context_impl(
            user_id=user_id,
            workspace_id=workspace_id,
            reference=reference,
            workbench_session_id=workbench_session_id,
            current_message=current_message,
            max_chars=max_chars,
            memory_max_chars=memory_max_chars,
            memory_task_key="",
        )

    async def context_for_chat(
        self,
        *,
        user_id: str,
        workspace_id: str,
        current_message: str = "",
        recent_messages: list[dict[str, Any]] | None = None,
        mentioned_files: list[str] | None = None,
        memory_task_key: str = "",
        max_chars: int = 9000,
        memory_max_chars: int = 3000,
    ) -> dict[str, Any]:
        """Compile authoritative Workspace context for an owned chat reasoning turn."""
        return await self._context_impl(
            user_id=user_id,
            workspace_id=workspace_id,
            current_message=current_message,
            recent_messages=recent_messages,
            mentioned_files=mentioned_files,
            memory_task_key=memory_task_key,
            max_chars=max_chars,
            memory_max_chars=memory_max_chars,
        )

    async def health(
        self,
        *,
        user_id: str,
        reference: str,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            allow_fuzzy=True,
        )
        return await classify_workspace_health(
            workspace=workspace,
            user_id=user_id,
            identity=await identity_for_user_id(user_id),
        )

    async def reconcile(
        self,
        *,
        user_id: str,
        reference: str,
    ) -> dict[str, Any]:
        workspace = await self._resolve(
            user_id=user_id,
            reference=reference,
            destructive=True,
            allow_fuzzy=False,
        )
        return await conservative_reconcile_workspace(
            workspace=workspace,
            user_id=user_id,
            identity=await identity_for_user_id(user_id),
        )

    async def groups(
        self,
        *,
        user_id: str,
        group_ref: str | None = None,
        group_type: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if group_ref:
            return {
                "group": await WorkspaceGroupService.get_group(
                    user_id=user_id,
                    group_ref=group_ref,
                )
            }
        groups = await WorkspaceGroupService.list_groups(
            user_id=user_id,
            group_type=group_type,
            limit=max(1, min(int(limit), 200)),
        )
        return {"groups": groups}

    async def execute(
        self,
        *,
        user_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        op = str(action or "").strip().lower()
        body = dict(payload or {})
        if op not in SUPPORTED_ACTIONS:
            raise WorkspaceActionError(
                "WORKSPACE_ACTION_UNSUPPORTED",
                f"unsupported Workspace OS action: {op or '<blank>'}",
                status_code=422,
                details={"supported_actions": sorted(SUPPORTED_ACTIONS)},
            )

        if op == "resolve":
            return await self.resolve(user_id=user_id, **body)
        if op == "context":
            return await self.context(user_id=user_id, **body)
        if op in {
            "health",
            "reconcile",
            "repositories",
            "instructions",
            "instruction_history",
            "instruction_preview",
            "instruction_save",
            "environment_profiles",
            "environment_versions",
            "environment_create",
            "environment_version_create",
            "environment_set_active_version",
            "environment_set_target",
            "checkpoints",
            "tasks",
            "task_summary",
            "task_create",
            "task_update_status",
            "task_pin_repository",
            "task_add_evidence",
            "workspace_update",
            "repository_add",
            "repository_update",
            "repository_remove",
        }:
            ref = body.pop("reference", None) or body.pop("workspace_id", None)
            body["reference"] = str(ref or "")

        try:
            if op == "health":
                return await self.health(user_id=user_id, **body)
            if op == "reconcile":
                return await self.reconcile(user_id=user_id, **body)
            if op == "repositories":
                return await self.repositories(user_id=user_id, **body)
            if op == "repository_catalog":
                return await self.repository_catalog(user_id=user_id)
            if op == "repository_add":
                return await self.repository_add(user_id=user_id, **body)
            if op == "repository_update":
                return await self.repository_update(user_id=user_id, **body)
            if op == "repository_remove":
                return await self.repository_remove(user_id=user_id, **body)
            if op == "instructions":
                return await self.instructions(user_id=user_id, **body)
            if op == "instruction_history":
                return await self.instruction_history(user_id=user_id, **body)
            if op == "instruction_preview":
                return await self.instruction_preview(user_id=user_id, **body)
            if op == "instruction_save":
                return await self.instruction_save(user_id=user_id, **body)
            if op == "environment_profiles":
                return await self.environment_profiles(user_id=user_id, **body)
            if op == "environment_versions":
                return await self.environment_versions(user_id=user_id, **body)
            if op == "environment_create":
                return await self.environment_create(user_id=user_id, **body)
            if op == "environment_version_create":
                return await self.environment_version_create(user_id=user_id, **body)
            if op == "environment_set_active_version":
                return await self.environment_set_active_version(user_id=user_id, **body)
            if op == "environment_set_target":
                return await self.environment_set_target(user_id=user_id, **body)
            if op == "checkpoints":
                return await self.checkpoints(user_id=user_id, **body)
            if op == "tasks":
                return await self.tasks(user_id=user_id, **body)
            if op == "task_summary":
                return await self.task_summary(user_id=user_id, **body)
            if op == "task_create":
                return await self.task_create(user_id=user_id, **body)
            if op == "task_update_status":
                return await self.task_update_status(user_id=user_id, **body)
            if op == "task_pin_repository":
                return await self.task_pin_repository(user_id=user_id, **body)
            if op == "task_add_evidence":
                return await self.task_add_evidence(user_id=user_id, **body)
            if op == "workspace_update":
                return await self.workspace_update(user_id=user_id, **body)
            if op == "groups":
                return await self.groups(user_id=user_id, **body)
            if op == "group_create":
                return {
                    "group": await WorkspaceGroupService.create_group(
                        user_id=user_id,
                        **body,
                    )
                }
            if op == "group_update":
                return {
                    "group": await WorkspaceGroupService.update_group(
                        user_id=user_id,
                        **body,
                    )
                }
            if op == "group_add_member":
                return {
                    "group": await WorkspaceGroupService.add_member(
                        user_id=user_id,
                        **body,
                    )
                }
            if op == "group_remove_member":
                return {
                    "group": await WorkspaceGroupService.remove_member(
                        user_id=user_id,
                        **body,
                    )
                }
            if op == "group_update_member":
                return {
                    "group": await WorkspaceGroupService.update_member(
                        user_id=user_id,
                        **body,
                    )
                }
            if op == "group_reorder":
                return {
                    "group": await WorkspaceGroupService.reorder_members(
                        user_id=user_id,
                        **body,
                    )
                }
        except WorkspaceGroupError as exc:
            raise _map_group_error(exc) from exc
        except WorkspaceInstructionError as exc:
            raise WorkspaceActionError(
                str(getattr(exc, "code", "WORKSPACE_INSTRUCTION_ERROR")),
                str(exc),
                status_code=int(getattr(exc, "status_code", 400)),
            ) from exc
        except WorkspaceTaskError as exc:
            raise WorkspaceActionError(
                str(exc.code),
                str(exc),
                status_code=int(exc.status_code),
            ) from exc
        except EnvironmentProfileNotFoundError as exc:
            raise WorkspaceActionError(
                "ENVIRONMENT_PROFILE_NOT_FOUND",
                str(exc),
                status_code=404,
            ) from exc
        except (EnvironmentProfileError, EnvironmentTargetError, ValueError) as exc:
            raise WorkspaceActionError(
                "ENVIRONMENT_PROFILE_INVALID",
                str(exc),
                status_code=422,
            ) from exc
        raise AssertionError(f"unreachable Workspace OS action: {op}")


workspace_action_service = WorkspaceActionService()


__all__ = [
    "READ_ACTIONS",
    "SUPPORTED_ACTIONS",
    "WRITE_ACTIONS",
    "WorkspaceActionError",
    "WorkspaceActionService",
    "workspace_action_service",
]
