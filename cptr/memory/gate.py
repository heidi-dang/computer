"""Control-plane execution gate for CPTR Memory Core."""

from __future__ import annotations

import re
from typing import Any

from cptr.memory.domain import MemoryContextBundle, PrepareContextInput
from cptr.memory.service import get_memory_service
from cptr.models import Workspace
from cptr.utils.db import get_db

GATED_CONTROL_SCOPES = frozenset(
    {
        "task:write",
        "autonomous:run",
        "coding:write",
        "command:execute",
    }
)
_WORKSPACE_PATH_RE = re.compile(r"/workspaces/([^/?#]+)")


async def _workspace_path_for_request(request: Any, user_id: str) -> str:
    path = str(getattr(getattr(request, "url", None), "path", "") or "")
    match = _WORKSPACE_PATH_RE.search(path)
    if not match:
        return ""
    workspace_id = match.group(1)
    async with await get_db() as db:
        workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.user_id != user_id:
        # Ownership/availability errors remain authoritative in the route itself.
        return ""
    return str(workspace.path or "")


async def require_control_action_memory(
    request: Any,
    *,
    user_id: str,
    required_scope: str | None,
) -> MemoryContextBundle | None:
    if required_scope not in GATED_CONTROL_SCOPES:
        return None
    workspace = await _workspace_path_for_request(request, user_id)
    method = str(getattr(request, "method", "") or "CONTROL").upper()
    path = str(getattr(getattr(request, "url", None), "path", "") or "control action")
    bundle = await get_memory_service().prepare_context(
        PrepareContextInput(
            user_id=user_id,
            workspace=workspace,
            task_key="",
            current_message=f"CPTR {required_scope} action: {method} {path}",
            max_chars=4_000,
            runtime_request=request,
        )
    )
    state = getattr(request, "state", None)
    if state is not None:
        state.memory_context_id = bundle.context_id
        state.memory_version = bundle.memory_version
        state.memory_status = bundle.status
    return bundle
