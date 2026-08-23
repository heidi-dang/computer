"""Scoped bearer authentication for the versioned Control API."""

from __future__ import annotations

import hmac
from typing import Any

from cptr.models import Auth
from cptr.utils.config import AuthResult


def _hash_key(raw: str) -> str:
    from cptr.routers.gateway import _hash_key as gateway_hash_key

    return gateway_hash_key(raw)


async def _get_api_keys() -> list[dict[str, Any]]:
    from cptr.routers.gateway import _get_api_keys as gateway_get_api_keys

    return await gateway_get_api_keys()


async def authenticate_control_request(request: Any, required_scope: str | None = None) -> str:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise PermissionError("missing control-plane bearer token")
    token = authorization[7:].strip()
    if not token:
        raise PermissionError("empty control-plane bearer token")
    token_hash = _hash_key(token)
    matched: dict[str, Any] | None = None
    for key in await _get_api_keys():
        key_hash = key.get("key_hash")
        if isinstance(key_hash, str) and hmac.compare_digest(key_hash, token_hash):
            matched = key
            break
    if matched is None or not matched.get("user_id"):
        raise PermissionError("invalid control-plane bearer token")

    scopes = {
        value.strip()
        for value in (matched.get("scopes") or matched.get("control_scopes") or [])
        if isinstance(value, str) and value.strip()
    }
    if required_scope and required_scope not in scopes:
        raise PermissionError(f"missing required scope: {required_scope}")

    user_id = str(matched["user_id"])
    auth_row = await Auth.get_by_user_id(user_id)
    request.state.auth = AuthResult(
        user_id=user_id,
        username=auth_row.username if auth_row else None,
        role="user",
    )
    request.state.control_scopes = scopes
    return user_id
