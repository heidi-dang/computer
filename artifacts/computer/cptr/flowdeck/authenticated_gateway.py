"""Authenticated FlowDeck specialist dispatch boundary.

All production specialist dispatch must enter here.  Identity is taken from
the authenticated CPTR request; request payload fields are untrusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from cptr.flowdeck.coding import (
    CodingRequest,
    _native_run_browser_debugger,
    _native_run_coding_specialist,
)
from cptr.flowdeck.execution import (
    READ_ONLY_SPECIALIST_IDS,
    MapperRequest,
    _native_run_read_only_specialist,
)
from cptr.models.workspaces import Workspace
from cptr.flowdeck.worktrees import validate_execution_worktree


class AuthenticatedGatewayError(RuntimeError):
    """Raised when authenticated specialist dispatch cannot be authorized."""


@dataclass(frozen=True)
class SpecialistDispatchRequest:
    role: str
    request_key: str
    task: str
    workspace: str
    model: str
    connection: dict[str, Any]
    parent_chat_id: str
    parent_message_id: str | None = None
    parent_flowdeck_run_id: str | None = None
    check: str = "tests"
    trusted_repository: bool = True
    repository_identity: str = ""
    timeout_seconds: float = 120
    execution_mode: str = "tool_calling"
    codeact_program: str | None = None
    execution_workspace: str | None = None
    branch_scope: str | None = None
    designer_operation: str | None = None
    designer_input: dict[str, Any] | None = None


def _auth_user_id(request: Any) -> str:
    auth = getattr(getattr(request, "state", None), "auth", None)
    user_id = getattr(auth, "user_id", None)
    if not isinstance(user_id, str) or not user_id:
        raise AuthenticatedGatewayError("authenticated CPTR request identity is required")
    return user_id


async def resolve_gateway_workspace(
    *,
    session_factory: Any,
    user_id: str,
    requested_workspace: str,
) -> str:
    """Resolve exactly one canonical workspace owned by the authenticated user."""
    try:
        requested = Path(requested_workspace).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuthenticatedGatewayError("workspace path is invalid") from exc
    if not requested.is_dir():
        raise AuthenticatedGatewayError("workspace is not a directory")
    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(Workspace).where(Workspace.user_id == user_id)
                )
            ).all()
        )
    matches = []
    for row in rows:
        try:
            if Path(row.path).expanduser().resolve() == requested:
                matches.append(row)
        except (OSError, RuntimeError, ValueError):
            continue
    if len(matches) != 1:
        raise AuthenticatedGatewayError(
            "workspace ownership is missing, stale, or ambiguous"
        )
    return str(requested)


async def dispatch_authenticated_specialist(
    request: Any,
    dispatch: SpecialistDispatchRequest,
    *,
    store: Any = None,
) -> str:
    """Authorize and dispatch one specialist through CPTR's native loop."""
    user_id = _auth_user_id(request)
    if store is None:
        from cptr.utils.db import get_session_factory

        session_factory = get_session_factory()
    else:
        session_factory = store.session_factory
    workspace = await resolve_gateway_workspace(
        session_factory=session_factory,
        user_id=user_id,
        requested_workspace=dispatch.workspace,
    )
    # Re-read ownership immediately before entering any native model/tool loop.
    workspace = await resolve_gateway_workspace(
        session_factory=session_factory,
        user_id=user_id,
        requested_workspace=workspace,
    )
    execution_workspace = workspace
    if dispatch.execution_workspace is not None:
        try:
            execution_workspace = str(
                await validate_execution_worktree(workspace, dispatch.execution_workspace)
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise AuthenticatedGatewayError("execution workspace is not an owned repository worktree") from exc
    if dispatch.role in {"backend-coder", "frontend-coder"}:
        return await _native_run_coding_specialist(
            CodingRequest(
                role=dispatch.role,
                workspace=execution_workspace,
                canonical_workspace=workspace,
                user_id=user_id,
                task=dispatch.task,
                request_key=dispatch.request_key,
                parent_message_id=dispatch.parent_message_id,
                branch_scope=dispatch.branch_scope,
            ),
            model=dispatch.model,
            connection=dispatch.connection,
            parent_chat_id=dispatch.parent_chat_id,
            parent_flowdeck_run_id=dispatch.parent_flowdeck_run_id,
            parent_message_id=dispatch.parent_message_id,
            store=store,
        )
    if dispatch.role == "browser-debugger":
        return await _native_run_browser_debugger(
            CodingRequest(
                role=dispatch.role,
                workspace=workspace,
                user_id=user_id,
                task=dispatch.task,
                request_key=dispatch.request_key,
                parent_message_id=dispatch.parent_message_id,
            ),
            model=dispatch.model,
            connection=dispatch.connection,
            parent_chat_id=dispatch.parent_chat_id,
            parent_flowdeck_run_id=dispatch.parent_flowdeck_run_id,
            parent_message_id=dispatch.parent_message_id,
            store=store,
        )
    if dispatch.role in READ_ONLY_SPECIALIST_IDS:
        return await _native_run_read_only_specialist(
            MapperRequest(
                request_key=dispatch.request_key,
                task=dispatch.task,
                workspace=workspace,
                user_id=user_id,
                model=dispatch.model,
                connection=dispatch.connection,
                parent_chat_id=dispatch.parent_chat_id,
                parent_message_id=dispatch.parent_message_id,
                parent_flowdeck_run_id=dispatch.parent_flowdeck_run_id,
                execution_mode=dispatch.execution_mode,
                codeact_program=dispatch.codeact_program,
                authenticated_request=request,
            ),
            dispatch.role,
            store=store,
        )
    if dispatch.role == "tester":
        from cptr.flowdeck.tester import TesterRequest, run_tester

        return str(
            await run_tester(
                TesterRequest(
                    request_key=dispatch.request_key,
                    workspace=execution_workspace,
                    user_id=user_id,
                    check=dispatch.check,
                    trusted_repository=dispatch.trusted_repository,
                    repository_identity=(
                        dispatch.repository_identity or f"authenticated-workspace:{workspace}"
                    ),
                    timeout_seconds=dispatch.timeout_seconds,
                ),
                store=store,
            )
        )
    if dispatch.role == "designer":
        import json
        from cptr.flowdeck.designer import DesignerRequest, run_designer

        return json.dumps(await run_designer(
            DesignerRequest(
                request_key=dispatch.request_key,
                operation=dispatch.designer_operation or "extract",
                workspace=workspace,
                user_id=user_id,
                input=dispatch.designer_input or {},
                parent_chat_id=dispatch.parent_chat_id,
            ),
            store=store,
        ))
    raise AuthenticatedGatewayError("specialist is not enabled")