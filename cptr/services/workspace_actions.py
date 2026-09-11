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
from cptr.models.workspaces import Workspace, WorkspaceAlias
from cptr.services.environment_profile import EnvironmentProfileService
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
from cptr.services.workspace_instructions import WorkspaceInstructionService
from cptr.services.workspace_resolver import (
    AmbiguousWorkspaceError,
    UnsafeResolutionError,
    WorkspaceNotFoundError,
    WorkspaceResolver,
)
from cptr.services.workbench_sessions import workbench_session_store
from cptr.utils.db import get_db
from cptr.utils.identity import identity_for_user_id
from cptr.utils.redaction import redact_sensitive


READ_ACTIONS = frozenset({"resolve", "context", "health", "groups"})
WRITE_ACTIONS = frozenset(
    {
        "reconcile",
        "group_create",
        "group_update",
        "group_add_member",
        "group_remove_member",
        "group_update_member",
        "group_reorder",
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
    ) -> tuple[Any | None, list[str]]:
        diagnostics: list[str] = []
        namespace = await resolve_workspace_namespace(user_id, str(workspace.id))
        try:
            bundle = await get_memory_service().prepare_context(
                PrepareContextInput(
                    user_id=user_id,
                    workspace=namespace.workspace_id or str(workspace.id),
                    task_key="",
                    current_message=current_message,
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
        if op == "health":
            ref = body.pop("reference", None) or body.pop("workspace_id", None)
            return await self.health(user_id=user_id, reference=str(ref or ""))
        if op == "reconcile":
            ref = body.pop("reference", None) or body.pop("workspace_id", None)
            return await self.reconcile(user_id=user_id, reference=str(ref or ""))
        try:
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
