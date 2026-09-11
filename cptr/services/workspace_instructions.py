"""Versioned Workspace Instructions service.

Provides immutable versioning, optimistic concurrency with expected_version,
content hashing, safe lookup, pointer resolution, owner scoping, history,
and canonical prompt preview generation without adding a second instruction engine.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from cptr.models.workspaces import Workspace, WorkspaceInstructionVersion
from cptr.services.workspace_refs import WorkspaceRefNotFound, resolve_workspace_ref
from cptr.utils.context import estimate_tokens
from cptr.utils.db import get_db


def _uuid() -> str:
    return str(uuid.uuid4())


class WorkspaceInstructionError(Exception):
    """Base error for workspace instruction operations."""

    status_code: int = 400
    code: str = "WORKSPACE_INSTRUCTION_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class WorkspaceNotFoundError(WorkspaceInstructionError):
    """Raised when workspace is not found."""

    status_code: int = 404
    code: str = "WORKSPACE_NOT_FOUND"


class WorkspaceAccessDeniedError(WorkspaceInstructionError):
    """Raised when user does not own or have access to the workspace."""

    status_code: int = 403
    code: str = "WORKSPACE_ACCESS_DENIED"


class WorkspaceInstructionNotFoundError(WorkspaceInstructionError):
    """Raised when a specific instruction version does not exist."""

    status_code: int = 404
    code: str = "INSTRUCTION_VERSION_NOT_FOUND"


class WorkspaceInstructionConflictError(WorkspaceInstructionError):
    """Raised when expected_version does not match the current version."""

    status_code: int = 409
    code: str = "INSTRUCTION_VERSION_CONFLICT"

    def __init__(
        self,
        *,
        workspace_id: str,
        current_version: int,
        expected_version: int,
        message: str | None = None,
    ) -> None:
        msg = (
            message
            or f"Instruction version conflict for workspace {workspace_id}: "
            f"expected {expected_version}, but current version is {current_version}"
        )
        super().__init__(msg)
        self.workspace_id = workspace_id
        self.current_version = current_version
        self.expected_version = expected_version


def compute_content_hash(content: str) -> str:
    """Compute SHA-256 hash of instructions content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def format_instructions_block(
    content: str,
    *,
    source: str = "workspace_instruction",
    version: int | None = None,
) -> str:
    """Canonical formatting of instructions for system prompt integration.

    Matches the established format in cptr/utils/prompt_templates.py
    without introducing a divergent format or second engine.
    """
    text = content.strip()
    if not text:
        return ""
    if source == "workspace_instruction":
        v_suffix = f" (v{version})" if version is not None else ""
        return (
            f"<instructions>\n{text}\n</instructions>\n\n"
            f"The above <instructions> were loaded from workspace instructions{v_suffix}. "
            "These instructions persist across sessions and are user-authored workspace instructions. "
            "Managed memory is shown separately when available."
        )
    return (
        f"<instructions>\n{text}\n</instructions>\n\n"
        "The above <instructions> were loaded from instruction files in the workspace root. "
        "These files persist across sessions and are user-authored workspace instructions. "
        "Managed memory is shown separately when available."
    )


@dataclass(frozen=True)
class CompiledWorkspaceInstructionPreview:
    """Compiled preview output for workspace instructions."""

    workspace_id: str
    version: int | None
    content: str
    content_hash: str | None
    compiled_instructions: str
    source: str  # "workspace_instruction", "instruction_files", "candidate", "none"
    char_count: int
    estimated_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "version": self.version,
            "content": self.content,
            "content_hash": self.content_hash,
            "compiled_instructions": self.compiled_instructions,
            "source": self.source,
            "char_count": self.char_count,
            "estimated_tokens": self.estimated_tokens,
        }


class WorkspaceInstructionService:
    """Core service for managing versioned workspace instructions."""

    async def _get_workspace(self, *, user_id: str, workspace_id: str) -> Workspace:
        """Resolve workspace and enforce owner scoping."""
        async with await get_db() as db:
            ws = await db.get(Workspace, workspace_id)
        if ws is not None:
            if str(ws.user_id) != str(user_id):
                raise WorkspaceAccessDeniedError(
                    f"Access denied: user {user_id} does not own workspace {workspace_id}"
                )
            return ws

        # Try resolving via stable ref (slug, alias, path)
        try:
            res = await resolve_workspace_ref(
                user_id=user_id,
                reference=workspace_id,
            )
            return res.workspace
        except WorkspaceRefNotFound:
            pass

        # Check if workspace exists under another user to distinguish 403 vs 404
        async with await get_db() as db:
            other_ws = await db.get(Workspace, workspace_id)
            if other_ws is not None and str(other_ws.user_id) != str(user_id):
                raise WorkspaceAccessDeniedError(
                    f"Access denied: user {user_id} does not own workspace {workspace_id}"
                )

        raise WorkspaceNotFoundError(f"Workspace not found: {workspace_id}")

    async def get_current_instruction(
        self,
        *,
        user_id: str,
        workspace_id: str,
    ) -> WorkspaceInstructionVersion | None:
        """Safe lookup of the current active instruction version.

        Uses the workspace pointer if present and valid; falls back safely
        to querying the latest active version.
        """
        workspace = await self._get_workspace(user_id=user_id, workspace_id=workspace_id)
        actual_ws_id = str(workspace.id)

        async with await get_db() as db:
            # 1. Pointer lookup if set
            if workspace.current_instruction_version_id:
                current = await db.get(
                    WorkspaceInstructionVersion,
                    workspace.current_instruction_version_id,
                )
                if (
                    current is not None
                    and str(current.workspace_id) == actual_ws_id
                    and str(current.user_id) == str(user_id)
                    and bool(current.is_current)
                ):
                    return current

            # 2. Safe lookup via is_current flag
            stmt = (
                select(WorkspaceInstructionVersion)
                .where(
                    WorkspaceInstructionVersion.workspace_id == actual_ws_id,
                    WorkspaceInstructionVersion.user_id == user_id,
                    WorkspaceInstructionVersion.is_current.is_(True),
                )
                .order_by(WorkspaceInstructionVersion.version.desc())
            )
            res = await db.execute(stmt)
            current = res.scalars().first()
            if current is not None:
                return current

            # 3. Fallback safe lookup: highest version number
            stmt_fallback = (
                select(WorkspaceInstructionVersion)
                .where(
                    WorkspaceInstructionVersion.workspace_id == actual_ws_id,
                    WorkspaceInstructionVersion.user_id == user_id,
                )
                .order_by(WorkspaceInstructionVersion.version.desc())
            )
            res_fallback = await db.execute(stmt_fallback)
            return res_fallback.scalars().first()

    async def get_instruction_version(
        self,
        *,
        user_id: str,
        workspace_id: str,
        version: int,
    ) -> WorkspaceInstructionVersion | None:
        """Fetch a specific historical instruction version with owner scoping."""
        workspace = await self._get_workspace(user_id=user_id, workspace_id=workspace_id)
        actual_ws_id = str(workspace.id)

        async with await get_db() as db:
            stmt = select(WorkspaceInstructionVersion).where(
                WorkspaceInstructionVersion.workspace_id == actual_ws_id,
                WorkspaceInstructionVersion.user_id == user_id,
                WorkspaceInstructionVersion.version == version,
            )
            res = await db.execute(stmt)
            return res.scalars().first()

    async def get_instruction_history(
        self,
        *,
        user_id: str,
        workspace_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WorkspaceInstructionVersion], int]:
        """Fetch historical instruction versions ordered by version descending."""
        workspace = await self._get_workspace(user_id=user_id, workspace_id=workspace_id)
        actual_ws_id = str(workspace.id)

        async with await get_db() as db:
            count_stmt = select(func.count(WorkspaceInstructionVersion.id)).where(
                WorkspaceInstructionVersion.workspace_id == actual_ws_id,
                WorkspaceInstructionVersion.user_id == user_id,
            )
            total = (await db.execute(count_stmt)).scalar_one() or 0

            stmt = (
                select(WorkspaceInstructionVersion)
                .where(
                    WorkspaceInstructionVersion.workspace_id == actual_ws_id,
                    WorkspaceInstructionVersion.user_id == user_id,
                )
                .order_by(WorkspaceInstructionVersion.version.desc())
                .limit(limit)
                .offset(offset)
            )
            res = await db.execute(stmt)
            return list(res.scalars().all()), total

    async def save_instruction_version(
        self,
        *,
        user_id: str,
        workspace_id: str,
        content: str,
        expected_version: int | None = None,
        change_summary: str | None = None,
    ) -> WorkspaceInstructionVersion:
        """Save a new version of workspace instructions with optimistic concurrency.

        - If expected_version is provided:
            0 requires no prior versions exist.
            N requires current version to be exactly N.
        - Calculates content_hash (SHA-256).
        - Sets previous versions to is_current=False.
        - Updates Workspace.current_instruction_version_id pointer.
        - Scoped strictly to the owner.
        """
        workspace = await self._get_workspace(user_id=user_id, workspace_id=workspace_id)
        actual_ws_id = str(workspace.id)

        content_hash = compute_content_hash(content)
        now = int(time.time())

        async with await get_db() as db:
            # Query current active version inside the transaction
            stmt = (
                select(WorkspaceInstructionVersion)
                .where(
                    WorkspaceInstructionVersion.workspace_id == actual_ws_id,
                    WorkspaceInstructionVersion.user_id == user_id,
                    WorkspaceInstructionVersion.is_current.is_(True),
                )
                .order_by(WorkspaceInstructionVersion.version.desc())
            )
            res = await db.execute(stmt)
            current = res.scalars().first()
            if current is None:
                # Fallback to highest version if none is flagged is_current
                stmt_fallback = (
                    select(WorkspaceInstructionVersion)
                    .where(
                        WorkspaceInstructionVersion.workspace_id == actual_ws_id,
                        WorkspaceInstructionVersion.user_id == user_id,
                    )
                    .order_by(WorkspaceInstructionVersion.version.desc())
                )
                res_fallback = await db.execute(stmt_fallback)
                current = res_fallback.scalars().first()

            current_version_num = int(current.version) if current is not None else 0

            if expected_version is not None and expected_version != current_version_num:
                raise WorkspaceInstructionConflictError(
                    workspace_id=actual_ws_id,
                    current_version=current_version_num,
                    expected_version=expected_version,
                )

            next_version = current_version_num + 1

            # Mark all existing versions for this workspace as not current
            await db.execute(
                update(WorkspaceInstructionVersion)
                .where(
                    WorkspaceInstructionVersion.workspace_id == actual_ws_id,
                    WorkspaceInstructionVersion.user_id == user_id,
                    WorkspaceInstructionVersion.is_current.is_(True),
                )
                .values(is_current=False)
            )

            new_record = WorkspaceInstructionVersion(
                id=_uuid(),
                workspace_id=actual_ws_id,
                user_id=user_id,
                version=next_version,
                content=content,
                content_hash=content_hash,
                is_current=True,
                change_summary=change_summary,
                created_at=now,
            )
            db.add(new_record)

            # Update workspace current version pointer
            ws_row = await db.get(Workspace, actual_ws_id)
            if ws_row is not None:
                ws_row.current_instruction_version_id = new_record.id
                ws_row.updated_at = now

            try:
                await db.commit()
                await db.refresh(new_record)
            except IntegrityError as exc:
                await db.rollback()
                raise WorkspaceInstructionConflictError(
                    workspace_id=actual_ws_id,
                    current_version=current_version_num,
                    expected_version=expected_version
                    if expected_version is not None
                    else current_version_num,
                    message=f"Concurrent instruction modification conflict: {exc}",
                ) from exc

            return new_record

    async def compile_preview(
        self,
        *,
        user_id: str,
        workspace_id: str,
        candidate_content: str | None = None,
    ) -> CompiledWorkspaceInstructionPreview:
        """Compile preview of workspace instructions.

        If candidate_content is provided, previews candidate text.
        Otherwise loads the current active instruction version.
        If no versioned instructions exist, falls back to instruction files
        (MEMORY.md, AGENTS.md, etc.) in the workspace root.
        """
        workspace = await self._get_workspace(user_id=user_id, workspace_id=workspace_id)
        actual_ws_id = str(workspace.id)

        if candidate_content is not None:
            c_hash = compute_content_hash(candidate_content)
            compiled = format_instructions_block(
                candidate_content,
                source="workspace_instruction",
                version=None,
            )
            return CompiledWorkspaceInstructionPreview(
                workspace_id=actual_ws_id,
                version=None,
                content=candidate_content,
                content_hash=c_hash,
                compiled_instructions=compiled,
                source="candidate",
                char_count=len(candidate_content),
                estimated_tokens=estimate_tokens(compiled),
            )

        current = await self.get_current_instruction(
            user_id=user_id,
            workspace_id=actual_ws_id,
        )
        if current is not None:
            compiled = format_instructions_block(
                current.content,
                source="workspace_instruction",
                version=int(current.version),
            )
            return CompiledWorkspaceInstructionPreview(
                workspace_id=actual_ws_id,
                version=int(current.version),
                content=current.content,
                content_hash=current.content_hash,
                compiled_instructions=compiled,
                source="workspace_instruction",
                char_count=len(current.content),
                estimated_tokens=estimate_tokens(compiled),
            )

        # Fallback to filesystem instructions if workspace has path
        file_instructions = ""
        if workspace.path:
            try:
                from cptr.utils.prompt_templates import _load_instruction_files

                file_instructions = _load_instruction_files(str(workspace.path))
            except Exception:
                file_instructions = ""

        if file_instructions:
            compiled = format_instructions_block(
                file_instructions,
                source="instruction_files",
            )
            return CompiledWorkspaceInstructionPreview(
                workspace_id=actual_ws_id,
                version=None,
                content=file_instructions,
                content_hash=compute_content_hash(file_instructions),
                compiled_instructions=compiled,
                source="instruction_files",
                char_count=len(file_instructions),
                estimated_tokens=estimate_tokens(compiled),
            )

        return CompiledWorkspaceInstructionPreview(
            workspace_id=actual_ws_id,
            version=None,
            content="",
            content_hash=None,
            compiled_instructions="",
            source="none",
            char_count=0,
            estimated_tokens=0,
        )


service = WorkspaceInstructionService()
