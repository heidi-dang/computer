"""Extended Gateway API endpoints for API key management.

GET  /api/gateway/keys/{key_id}          – get metadata for a single API key
PUT  /api/gateway/keys/{key_id}          – rename or update scopes/quota
POST /api/gateway/keys/{key_id}/rotate   – rotate (regenerate the secret of) an API key
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from cptr.routers.admin import require_admin
from cptr.services.api_keys import (
    list_api_keys,
    save_api_keys,
    clear_api_key_cache,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gateway", tags=["gateway-extended"])


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _mask_key(entry: dict) -> dict:
    """Return entry with key_hash removed (never expose secrets)."""
    return {k: v for k, v in entry.items() if k != "key_hash"}


class UpdateKeyRequest(BaseModel):
    name: Optional[str] = None
    scopes: Optional[list[str]] = None


# ── Get single key ────────────────────────────────────────────────────────────


@router.get("/keys/{key_id}")
async def get_api_key(request: Request, key_id: str):
    """Get metadata for a single API key (last used, scopes, name)."""
    require_admin(request)
    keys = await list_api_keys()
    key = next((k for k in keys if k.get("id") == key_id), None)
    if key is None:
        raise HTTPException(404, f"API key '{key_id}' not found")
    return _mask_key(key)


# ── Update key ────────────────────────────────────────────────────────────────


@router.put("/keys/{key_id}")
async def update_api_key(request: Request, key_id: str, body: UpdateKeyRequest):
    """Rename or update scopes for an existing API key."""
    require_admin(request)
    keys = await list_api_keys()
    key = next((k for k in keys if k.get("id") == key_id), None)
    if key is None:
        raise HTTPException(404, f"API key '{key_id}' not found")

    if body.name is not None:
        key["name"] = body.name.strip() or key["name"]
    if body.scopes is not None:
        key["scopes"] = [s.strip() for s in body.scopes if isinstance(s, str) and s.strip()]

    updated_keys = [k if k.get("id") != key_id else key for k in keys]
    await save_api_keys(updated_keys)
    return {"ok": True, **_mask_key(key)}


# ── Rotate key ────────────────────────────────────────────────────────────────


@router.post("/keys/{key_id}/rotate")
async def rotate_api_key(request: Request, key_id: str):
    """Rotate (regenerate the secret of) an existing API key without changing its ID or config."""
    require_admin(request)
    keys = await list_api_keys()
    key = next((k for k in keys if k.get("id") == key_id), None)
    if key is None:
        raise HTTPException(404, f"API key '{key_id}' not found")

    # Generate a new random secret
    new_secret = f"sk-{secrets.token_urlsafe(32)}"
    new_hash = _hash_key(new_secret)

    key["key_hash"] = new_hash
    updated_keys = [k if k.get("id") != key_id else key for k in keys]
    await save_api_keys(updated_keys)

    return {
        "ok": True,
        "key_id": key_id,
        "name": key.get("name"),
        "secret": new_secret,  # Return once — never stored in plaintext
        "note": "Store this secret immediately; it will not be shown again.",
    }
