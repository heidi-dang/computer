"""WorkspaceGroup and WorkspaceGroupMember domain and service.

Provides first-class Workspace OS grouping for multi-repo / cross-repo workspace
relationships.

Architecture notice:
WorkspaceGroup represents structural multi-repo/workspace relationships (such
as coordinating backend, frontend, companion plugins, and extensions).
Group identity and membership lifecycle are strictly decoupled from task execution,
transient worker leases, and task aggregation states.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import delete, select, update

from cptr.models.workspaces import (
    Workspace,
    WorkspaceAlias,
    WorkspaceGroup,
    WorkspaceGroupMember,
    _allocate_workspace_group_slug,
    _slugify,
)
from cptr.services.workspace_availability import is_workspace_available
from cptr.services.workspace_refs import (
    WorkspaceRefAmbiguous,
    WorkspaceRefNotFound,
    choose_workspace_ref,
)
from cptr.utils.db import get_db


class WorkspaceGroupError(ValueError):
    """Base exception for WorkspaceGroup operations."""

    code = "WORKSPACE_GROUP_ERROR"


class WorkspaceGroupNotFoundError(WorkspaceGroupError):
    code = "WORKSPACE_GROUP_NOT_FOUND"


class WorkspaceGroupDuplicateMemberError(WorkspaceGroupError):
    code = "DUPLICATE_GROUP_MEMBER"


class WorkspaceGroupSlugConflictError(WorkspaceGroupError):
    code = "WORKSPACE_GROUP_SLUG_CONFLICT"


class WorkspaceGroupValidationError(WorkspaceGroupError):
    code = "WORKSPACE_GROUP_VALIDATION_ERROR"


class WorkspaceGroupWorkspaceNotFoundError(WorkspaceGroupError):
    code = "WORKSPACE_NOT_FOUND"


def _now() -> int:
    return int(time.time())


def _uuid() -> str:
    return str(uuid.uuid4())


async def _resolve_workspace_record(db: Any, user_id: str, workspace_ref: str) -> Workspace:
    """Resolve a workspace by ID, slug, alias, path, or name, strictly owner-scoped."""
    ref = str(workspace_ref or "").strip()
    if not ref:
        raise WorkspaceGroupValidationError("workspace reference is required")

    # 1. Direct ID fast path
    ws = await db.get(Workspace, ref)
    if ws and ws.user_id == user_id:
        return ws

    workspaces = list(
        (await db.scalars(select(Workspace).where(Workspace.user_id == user_id))).all()
    )
    aliases = list(
        (await db.scalars(select(WorkspaceAlias).where(WorkspaceAlias.user_id == user_id))).all()
    )

    try:
        resolution = choose_workspace_ref(
            reference=ref,
            workspaces=workspaces,
            aliases=aliases,
        )
        return resolution.workspace
    except WorkspaceRefNotFound as exc:
        raise WorkspaceGroupWorkspaceNotFoundError(f"workspace not found: {workspace_ref}") from exc
    except WorkspaceRefAmbiguous as exc:
        raise WorkspaceGroupValidationError(str(exc)) from exc


async def _get_group_record(db: Any, user_id: str, group_ref: str) -> WorkspaceGroup:
    """Get group record by id or slug, strictly owner-scoped."""
    ref = str(group_ref or "").strip()
    if not ref:
        raise WorkspaceGroupValidationError("group reference is required")

    # 1. Direct ID lookup
    group = await db.get(WorkspaceGroup, ref)
    if group and group.user_id == user_id:
        return group

    # 2. Slug lookup
    result = await db.execute(
        select(WorkspaceGroup).where(WorkspaceGroup.user_id == user_id, WorkspaceGroup.slug == ref)
    )
    group = result.scalar_one_or_none()
    if group:
        return group

    raise WorkspaceGroupNotFoundError(f"workspace group not found: {group_ref}")


def _serialize_workspace(workspace: Workspace | None) -> dict[str, Any] | None:
    if workspace is None:
        return None
    return {
        "id": str(workspace.id),
        "name": str(workspace.name),
        "slug": str(workspace.slug) if workspace.slug else None,
        "path": str(workspace.path),
        "workspace_type": str(workspace.workspace_type or "project"),
        "available": is_workspace_available(workspace),
        "created_at": int(workspace.created_at),
        "updated_at": int(workspace.updated_at) if workspace.updated_at else None,
    }


def _serialize_member(
    member: WorkspaceGroupMember, workspace: Workspace | None = None
) -> dict[str, Any]:
    return {
        "id": str(member.id),
        "group_id": str(member.group_id),
        "workspace_id": str(member.workspace_id),
        "sort_order": int(member.sort_order),
        "role": str(member.role),
        "primary": bool(member.primary),
        "alias": str(member.alias) if member.alias else None,
        "enabled": bool(member.enabled),
        "config": dict(member.config or {}),
        "created_at": int(member.created_at),
        "updated_at": int(member.updated_at),
        "workspace": _serialize_workspace(workspace),
    }


def _serialize_group(
    group: WorkspaceGroup, members: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    members_list = members or []
    return {
        "id": str(group.id),
        "user_id": str(group.user_id),
        "name": str(group.name),
        "slug": str(group.slug),
        "description": str(group.description) if group.description else None,
        "group_type": str(group.group_type or "cross-repo"),
        "config": dict(group.config or {}),
        "created_at": int(group.created_at),
        "updated_at": int(group.updated_at),
        "member_count": len(members_list),
        "members": members_list,
    }


class WorkspaceGroupService:
    """Service providing owner-scoped CRUD and membership operations for WorkspaceGroups."""

    @staticmethod
    async def create_group(
        *,
        user_id: str,
        name: str,
        slug: str | None = None,
        description: str | None = None,
        group_type: str = "cross-repo",
        config: dict[str, Any] | None = None,
        members: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a new workspace group with optional initial ordered members."""
        clean_name = str(name or "").strip()
        if not clean_name:
            raise WorkspaceGroupValidationError("group name is required")

        group_id = _uuid()
        now = _now()

        async with await get_db() as db:
            # Slug assignment & duplicate check
            if slug:
                clean_slug = _slugify(slug)
                existing_slug = (
                    await db.scalars(
                        select(WorkspaceGroup.id).where(
                            WorkspaceGroup.user_id == user_id,
                            WorkspaceGroup.slug == clean_slug,
                        )
                    )
                ).first()
                if existing_slug:
                    raise WorkspaceGroupSlugConflictError(
                        f"group slug already in use: {clean_slug}"
                    )
                allocated_slug = clean_slug
            else:
                allocated_slug = await _allocate_workspace_group_slug(
                    db, user_id, clean_name, group_id
                )

            group = WorkspaceGroup(
                id=group_id,
                user_id=user_id,
                name=clean_name,
                slug=allocated_slug,
                description=str(description).strip() if description else None,
                group_type=str(group_type or "cross-repo").strip(),
                config=dict(config or {}),
                created_at=now,
                updated_at=now,
            )
            db.add(group)

            member_dicts: list[dict[str, Any]] = []
            if members:
                seen_workspace_ids: set[str] = set()
                resolved_workspaces: list[Workspace] = []

                for m in members:
                    ws_ref = str(m.get("workspace_id") or m.get("workspace_ref") or "").strip()
                    ws = await _resolve_workspace_record(db, user_id, ws_ref)
                    if ws.id in seen_workspace_ids:
                        raise WorkspaceGroupDuplicateMemberError(
                            f"duplicate workspace in member list: {ws.id}"
                        )
                    seen_workspace_ids.add(ws.id)
                    resolved_workspaces.append(ws)

                has_primary = False
                for idx, (m, ws) in enumerate(zip(members, resolved_workspaces)):
                    is_primary = bool(m.get("primary", False))
                    if is_primary:
                        if has_primary:
                            raise WorkspaceGroupValidationError(
                                "only one member in a workspace group can be marked as primary"
                            )
                        has_primary = True

                    sort_order = m.get("sort_order")
                    if sort_order is None:
                        sort_order = idx

                    member_obj = WorkspaceGroupMember(
                        id=_uuid(),
                        group_id=group_id,
                        workspace_id=ws.id,
                        sort_order=int(sort_order),
                        role=str(m.get("role") or "member").strip(),
                        primary=is_primary,
                        alias=str(m.get("alias")).strip() if m.get("alias") else None,
                        enabled=bool(m.get("enabled", True)),
                        config=dict(m.get("config") or {}),
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(member_obj)
                    member_dicts.append(_serialize_member(member_obj, ws))

            await db.commit()

            # Ensure members are sorted deterministically
            member_dicts.sort(key=lambda m: (m["sort_order"], m["created_at"], m["id"]))
            return _serialize_group(group, member_dicts)

    @staticmethod
    async def get_group(
        *,
        user_id: str,
        group_ref: str,
    ) -> dict[str, Any]:
        """Fetch a group with its ordered members and resolved workspace metadata."""
        async with await get_db() as db:
            group = await _get_group_record(db, user_id, group_ref)

            # Query members in deterministic order: sort_order ASC, created_at ASC, id ASC
            stmt = (
                select(WorkspaceGroupMember)
                .where(WorkspaceGroupMember.group_id == group.id)
                .order_by(
                    WorkspaceGroupMember.sort_order.asc(),
                    WorkspaceGroupMember.created_at.asc(),
                    WorkspaceGroupMember.id.asc(),
                )
            )
            members = list((await db.scalars(stmt)).all())

            workspaces_by_id: dict[str, Workspace] = {}
            if members:
                ws_ids = [m.workspace_id for m in members]
                ws_stmt = select(Workspace).where(
                    Workspace.user_id == user_id, Workspace.id.in_(ws_ids)
                )
                workspaces = list((await db.scalars(ws_stmt)).all())
                workspaces_by_id = {ws.id: ws for ws in workspaces}

            serialized_members = [
                _serialize_member(m, workspaces_by_id.get(m.workspace_id)) for m in members
            ]
            return _serialize_group(group, serialized_members)

    @staticmethod
    async def list_groups(
        *,
        user_id: str,
        group_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List groups owned by user with deterministic ordering and member summaries."""
        async with await get_db() as db:
            query = select(WorkspaceGroup).where(WorkspaceGroup.user_id == user_id)
            if group_type:
                query = query.where(WorkspaceGroup.group_type == group_type)
            query = (
                query.order_by(WorkspaceGroup.created_at.asc(), WorkspaceGroup.id.asc())
                .offset(offset)
                .limit(limit)
            )

            groups = list((await db.scalars(query)).all())
            if not groups:
                return []

            group_ids = [g.id for g in groups]
            m_stmt = (
                select(WorkspaceGroupMember)
                .where(WorkspaceGroupMember.group_id.in_(group_ids))
                .order_by(
                    WorkspaceGroupMember.sort_order.asc(),
                    WorkspaceGroupMember.created_at.asc(),
                    WorkspaceGroupMember.id.asc(),
                )
            )
            all_members = list((await db.scalars(m_stmt)).all())

            ws_ids = list({m.workspace_id for m in all_members})
            workspaces_by_id: dict[str, Workspace] = {}
            if ws_ids:
                ws_stmt = select(Workspace).where(
                    Workspace.user_id == user_id, Workspace.id.in_(ws_ids)
                )
                workspaces = list((await db.scalars(ws_stmt)).all())
                workspaces_by_id = {ws.id: ws for ws in workspaces}

            members_by_group: dict[str, list[dict[str, Any]]] = {g.id: [] for g in groups}
            for m in all_members:
                members_by_group[m.group_id].append(
                    _serialize_member(m, workspaces_by_id.get(m.workspace_id))
                )

            return [_serialize_group(g, members_by_group.get(g.id, [])) for g in groups]

    @staticmethod
    async def update_group(
        *,
        user_id: str,
        group_ref: str,
        name: str | None = None,
        slug: str | None = None,
        description: str | None = None,
        group_type: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update group details."""
        async with await get_db() as db:
            group = await _get_group_record(db, user_id, group_ref)
            now = _now()

            if name is not None:
                clean_name = str(name).strip()
                if not clean_name:
                    raise WorkspaceGroupValidationError("group name cannot be blank")
                group.name = clean_name

            if slug is not None:
                clean_slug = _slugify(slug)
                if clean_slug != group.slug:
                    existing = (
                        await db.scalars(
                            select(WorkspaceGroup.id).where(
                                WorkspaceGroup.user_id == user_id,
                                WorkspaceGroup.slug == clean_slug,
                                WorkspaceGroup.id != group.id,
                            )
                        )
                    ).first()
                    if existing:
                        raise WorkspaceGroupSlugConflictError(
                            f"group slug already in use: {clean_slug}"
                        )
                    group.slug = clean_slug

            if description is not None:
                group.description = str(description).strip() if description else None

            if group_type is not None:
                group.group_type = str(group_type).strip()

            if config is not None:
                group.config = dict(config)

            group.updated_at = now
            await db.commit()

        return await WorkspaceGroupService.get_group(user_id=user_id, group_ref=group.id)

    @staticmethod
    async def delete_group(
        *,
        user_id: str,
        group_ref: str,
    ) -> dict[str, Any]:
        """Delete group and cascade-remove members."""
        async with await get_db() as db:
            group = await _get_group_record(db, user_id, group_ref)
            group_id = str(group.id)
            await db.execute(
                delete(WorkspaceGroupMember).where(WorkspaceGroupMember.group_id == group_id)
            )
            await db.execute(
                delete(WorkspaceGroup).where(
                    WorkspaceGroup.id == group_id, WorkspaceGroup.user_id == user_id
                )
            )
            await db.commit()
            return {"deleted": True, "id": group_id}

    @staticmethod
    async def add_member(
        *,
        user_id: str,
        group_ref: str,
        workspace_ref: str,
        role: str = "member",
        primary: bool = False,
        alias: str | None = None,
        sort_order: int | None = None,
        enabled: bool = True,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add a member to a group with duplicate protection and deterministic ordering."""
        now = _now()
        async with await get_db() as db:
            group = await _get_group_record(db, user_id, group_ref)
            workspace = await _resolve_workspace_record(db, user_id, workspace_ref)

            # Duplicate protection: verify workspace not already in group
            existing = (
                await db.scalars(
                    select(WorkspaceGroupMember).where(
                        WorkspaceGroupMember.group_id == group.id,
                        WorkspaceGroupMember.workspace_id == workspace.id,
                    )
                )
            ).first()
            if existing:
                raise WorkspaceGroupDuplicateMemberError(
                    f"workspace '{workspace.name}' ({workspace.id}) is already a member of group '{group.name}'"
                )

            # Primary uniqueness: if primary is True, unset existing primaries
            if primary:
                await db.execute(
                    update(WorkspaceGroupMember)
                    .where(WorkspaceGroupMember.group_id == group.id)
                    .values(primary=False, updated_at=now)
                )

            # Deterministic ordering: if not provided, place at end
            if sort_order is None:
                max_order = (
                    await db.scalars(
                        select(WorkspaceGroupMember.sort_order)
                        .where(WorkspaceGroupMember.group_id == group.id)
                        .order_by(WorkspaceGroupMember.sort_order.desc())
                    )
                ).first()
                sort_order = 0 if max_order is None else max_order + 1

            member = WorkspaceGroupMember(
                id=_uuid(),
                group_id=group.id,
                workspace_id=workspace.id,
                sort_order=int(sort_order),
                role=str(role or "member").strip(),
                primary=bool(primary),
                alias=str(alias).strip() if alias else None,
                enabled=bool(enabled),
                config=dict(config or {}),
                created_at=now,
                updated_at=now,
            )
            db.add(member)
            group.updated_at = now
            await db.commit()

        return await WorkspaceGroupService.get_group(user_id=user_id, group_ref=group.id)

    @staticmethod
    async def remove_member(
        *,
        user_id: str,
        group_ref: str,
        workspace_ref: str,
    ) -> dict[str, Any]:
        """Remove a member from a group."""
        async with await get_db() as db:
            group = await _get_group_record(db, user_id, group_ref)
            workspace = await _resolve_workspace_record(db, user_id, workspace_ref)

            result = await db.execute(
                delete(WorkspaceGroupMember).where(
                    WorkspaceGroupMember.group_id == group.id,
                    WorkspaceGroupMember.workspace_id == workspace.id,
                )
            )
            if result.rowcount == 0:
                raise WorkspaceGroupNotFoundError(
                    f"workspace '{workspace.name}' is not a member of group '{group.name}'"
                )

            group.updated_at = _now()
            await db.commit()

        return await WorkspaceGroupService.get_group(user_id=user_id, group_ref=group.id)

    @staticmethod
    async def update_member(
        *,
        user_id: str,
        group_ref: str,
        workspace_ref: str,
        role: str | None = None,
        primary: bool | None = None,
        alias: str | None = None,
        sort_order: int | None = None,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update a member's attributes within a group."""
        now = _now()
        async with await get_db() as db:
            group = await _get_group_record(db, user_id, group_ref)
            workspace = await _resolve_workspace_record(db, user_id, workspace_ref)

            member = (
                await db.scalars(
                    select(WorkspaceGroupMember).where(
                        WorkspaceGroupMember.group_id == group.id,
                        WorkspaceGroupMember.workspace_id == workspace.id,
                    )
                )
            ).first()
            if not member:
                raise WorkspaceGroupNotFoundError(
                    f"workspace '{workspace.name}' is not a member of group '{group.name}'"
                )

            if primary is not None:
                if primary:
                    # Unset primary from all other members
                    await db.execute(
                        update(WorkspaceGroupMember)
                        .where(
                            WorkspaceGroupMember.group_id == group.id,
                            WorkspaceGroupMember.id != member.id,
                        )
                        .values(primary=False, updated_at=now)
                    )
                member.primary = bool(primary)

            if role is not None:
                member.role = str(role).strip()

            if alias is not None:
                member.alias = str(alias).strip() if alias else None

            if sort_order is not None:
                member.sort_order = int(sort_order)

            if enabled is not None:
                member.enabled = bool(enabled)

            if config is not None:
                member.config = dict(config)

            member.updated_at = now
            group.updated_at = now
            await db.commit()

        return await WorkspaceGroupService.get_group(user_id=user_id, group_ref=group.id)

    @staticmethod
    async def reorder_members(
        *,
        user_id: str,
        group_ref: str,
        ordered_workspace_refs: list[str],
    ) -> dict[str, Any]:
        """Deterministically reorder members of a group based on the given list of workspace refs."""
        if not ordered_workspace_refs:
            raise WorkspaceGroupValidationError("ordered workspace list cannot be empty")

        now = _now()
        async with await get_db() as db:
            group = await _get_group_record(db, user_id, group_ref)

            # Resolve all workspace refs and check for duplicates in the list
            resolved_ids: list[str] = []
            seen_ids: set[str] = set()
            for ref in ordered_workspace_refs:
                ws = await _resolve_workspace_record(db, user_id, ref)
                if ws.id in seen_ids:
                    raise WorkspaceGroupValidationError(
                        f"duplicate workspace in reorder list: {ws.id}"
                    )
                seen_ids.add(ws.id)
                resolved_ids.append(ws.id)

            # Get all current members
            existing_members = list(
                (
                    await db.scalars(
                        select(WorkspaceGroupMember).where(
                            WorkspaceGroupMember.group_id == group.id
                        )
                    )
                ).all()
            )
            existing_member_map = {m.workspace_id: m for m in existing_members}

            # Verify every ID in resolved_ids is an actual member
            for ws_id in resolved_ids:
                if ws_id not in existing_member_map:
                    raise WorkspaceGroupValidationError(
                        f"workspace {ws_id} is not currently a member of group {group.name}"
                    )

            # Assign sort_order = index for specified members
            for index, ws_id in enumerate(resolved_ids):
                member = existing_member_map[ws_id]
                member.sort_order = index
                member.updated_at = now

            # If there are remaining members not specified in the list, place them after
            next_order = len(resolved_ids)
            for m in existing_members:
                if m.workspace_id not in seen_ids:
                    m.sort_order = next_order
                    m.updated_at = now
                    next_order += 1

            group.updated_at = now
            await db.commit()

        return await WorkspaceGroupService.get_group(user_id=user_id, group_ref=group.id)
