"""Control-plane execution gate for CPTR Memory Core."""

from __future__ import annotations

import os
import re
import time
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
_FAST_GATE_SCOPES = frozenset({"coding:write", "command:execute"})


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)) or str(default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


_GATE_CACHE_TTL_MS = _bounded_env_int("CPTR_MEMORY_GATE_CACHE_TTL_MS", 20_000, 0, 60_000)
_GATE_CACHE_MAX_ENTRIES = _bounded_env_int("CPTR_MEMORY_GATE_CACHE_MAX_ENTRIES", 256, 16, 4096)
_gate_cache: dict[
    tuple[str, str, str],
    tuple[float, int, tuple[object, ...], MemoryContextBundle],
] = {}


def _settings_signature(settings: dict[str, Any]) -> tuple[object, ...]:
    return (
        bool(settings.get("enabled", True)),
        bool(settings.get("required_for_execution", True)),
        int(settings.get("context_char_limit") or 9000),
        int(settings.get("canonical_char_limit") or 3000),
        int(settings.get("verification_ttl_seconds") or 0),
    )


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
    service = get_memory_service()

    bundle: MemoryContextBundle
    if required_scope in _FAST_GATE_SCOPES and _GATE_CACHE_TTL_MS > 0:
        # The direct coding/command routes use the Memory Core as a fail-closed
        # execution prerequisite; they do not consume the rendered recall text.
        # Reuse a recent prepared gate only after both settings and the durable
        # namespace version have been revalidated, so memory mutations or config
        # changes cannot silently reuse stale authorization context.
        from cptr.utils.memory import get_memory_settings

        settings = await get_memory_settings()
        settings_signature = _settings_signature(settings)
        memory_version = await service.store.namespace_version(user_id, workspace)
        cache_key = (user_id, workspace, required_scope)
        cached = _gate_cache.get(cache_key)
        now = time.monotonic()
        if (
            cached is not None
            and (now - cached[0]) * 1000 < _GATE_CACHE_TTL_MS
            and cached[1] == memory_version
            and cached[2] == settings_signature
        ):
            bundle = cached[3]
        else:
            bundle = await service.prepare_context(
                PrepareContextInput(
                    user_id=user_id,
                    workspace=workspace,
                    task_key="",
                    current_message=f"CPTR {required_scope} control action",
                    max_chars=4_000,
                    runtime_request=request,
                )
            )
            if len(_gate_cache) >= _GATE_CACHE_MAX_ENTRIES and cache_key not in _gate_cache:
                oldest_key = min(_gate_cache, key=lambda key: _gate_cache[key][0])
                _gate_cache.pop(oldest_key, None)
            _gate_cache[cache_key] = (now, bundle.memory_version, settings_signature, bundle)
    else:
        bundle = await service.prepare_context(
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
