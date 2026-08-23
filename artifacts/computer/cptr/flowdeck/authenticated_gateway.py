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
    run_browser_debugger,
    run_coding_specialist,
)
from cptr.flowdeck.execution import (
    READ_ONLY_SPECIALIST_IDS,
    MapperRequest,
    run_read_only_specialist,
)
from cptr.models.workspaces import Workspace


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
    if dispatch.role in {"backend-coder", "frontend-coder"}:
        return await run_coding_specialist(
            CodingRequest(
                role=dispatch.role,
                workspace=workspace,
                user_id=user_id,
                task=dispatch.task,
                request_key=dispatch.request_key,
            ),
            model=dispatch.model,
            connection=dispatch.connection,
            parent_chat_id=dispatch.parent_chat_id,
            store=store,
        )
    if dispatch.role == "browser-debugger":
        return await run_browser_debugger(
            CodingRequest(
                role=dispatch.role,
                workspace=workspace,
                user_id=user_id,
                task=dispatch.task,
                request_key=dispatch.request_key,
            ),
            model=dispatch.model,
            connection=dispatch.connection,
            parent_chat_id=dispatch.parent_chat_id,
            store=store,
        )
    if dispatch.role in READ_ONLY_SPECIALIST_IDS:
        return await run_read_only_specialist(
            MapperRequest(
                request_key=dispatch.request_key,
                task=dispatch.task,
                workspace=workspace,
                user_id=user_id,
                model=dispatch.model,
                connection=dispatch.connection,
                parent_chat_id=dispatch.parent_chat_id,
            ),
            dispatch.role,
            store=store,
        )
    raise AuthenticatedGatewayError("specialist is not enabled")