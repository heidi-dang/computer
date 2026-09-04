"""Scoped bearer authentication for the versioned Control API."""

from __future__ import annotations

import hashlib
from typing import Any, NoReturn

from fastapi import HTTPException

from cptr.memory.gate import require_control_action_memory
from cptr.memory.service import MemoryUnavailableError
from cptr.services.api_keys import list_api_keys, resolve_api_key_principal
from cptr.utils.config import AuthResult


class ControlMemoryUnavailable(PermissionError):
    """Authenticated control action was blocked because required memory was unavailable."""


def raise_control_auth_error(exc: PermissionError) -> NoReturn:
    """Map every Control API authentication failure to one structured HTTP contract."""
    message = str(exc)
    if isinstance(exc, ControlMemoryUnavailable):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MEMORY_REQUIRED",
                "message": message,
                "retriable": True,
            },
        ) from exc
    if message.startswith("missing required scope"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CONTROL_SCOPE_REQUIRED",
                "message": message,
                "retriable": False,
            },
        ) from exc
    raise HTTPException(
        status_code=401,
        detail={
            "code": "CONTROL_AUTH_FAILED",
            "message": "control-plane authentication failed",
            "retriable": False,
        },
    ) from exc


async def require_control_user(request: Any, required_scope: str | None = None) -> str:
    """Authenticate a control request and apply the shared HTTP error mapping."""
    try:
        return await authenticate_control_request(request, required_scope)
    except PermissionError as exc:
        raise_control_auth_error(exc)


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _get_api_keys() -> list[dict[str, Any]]:
    """Compatibility wrapper retained for callers/tests that inventory API keys."""
    return await list_api_keys()


async def authenticate_control_request(request: Any, required_scope: str | None = None) -> str:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise PermissionError("missing control-plane bearer token")
    token = authorization[7:].strip()
    if not token:
        raise PermissionError("empty control-plane bearer token")

    principal = await resolve_api_key_principal(_hash_key(token))
    if principal is None:
        raise PermissionError("invalid control-plane bearer token")
    if required_scope and required_scope not in principal.scopes:
        raise PermissionError(f"missing required scope: {required_scope}")

    request.state.auth = AuthResult(
        user_id=principal.user_id,
        username=principal.username,
        role="user",
    )
    request.state.control_scopes = set(principal.scopes)
    try:
        await require_control_action_memory(
            request,
            user_id=principal.user_id,
            required_scope=required_scope,
        )
    except MemoryUnavailableError as exc:
        raise ControlMemoryUnavailable("required CPTR memory context is unavailable") from exc
    return principal.user_id
