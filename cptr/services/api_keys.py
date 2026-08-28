"""Indexed API-key storage and bounded authentication principal cache."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select

from cptr.env import CONTROL_AUTH_CACHE_MAX_ENTRIES, CONTROL_AUTH_CACHE_TTL_SECONDS
from cptr.models import Auth, Config, ControlApiKey
from cptr.utils.db import get_db


@dataclass(frozen=True)
class ApiKeyPrincipal:
    user_id: str
    username: str | None
    scopes: frozenset[str]


_principal_cache: OrderedDict[str, tuple[float, ApiKeyPrincipal]] = OrderedDict()
_migration_lock = asyncio.Lock()


def clear_api_key_cache() -> None:
    _principal_cache.clear()


def _entry_dict(row: ControlApiKey) -> dict[str, Any]:
    return {
        "id": row.id,
        "key_hash": row.key_hash,
        "user_id": row.user_id,
        "name": row.name,
        "scopes": list(row.scopes or []),
        "created_at": int(row.created_at),
    }


async def _legacy_keys() -> list[dict[str, Any]]:
    value = await Config.get("api_keys")
    return [dict(item) for item in value] if isinstance(value, list) else []


async def migrate_legacy_api_keys() -> int:
    """Populate the indexed table once from the historical Config JSON value."""
    async with _migration_lock:
        async with await get_db() as db:
            existing = await db.scalar(select(ControlApiKey.id).limit(1))
        if existing is not None:
            return 0
        legacy = await _legacy_keys()
        if not legacy:
            return 0
        await _replace_table(legacy)
        return len(legacy)


async def list_api_keys() -> list[dict[str, Any]]:
    await migrate_legacy_api_keys()
    async with await get_db() as db:
        rows = (
            await db.scalars(
                select(ControlApiKey).order_by(
                    ControlApiKey.created_at.asc(), ControlApiKey.id.asc()
                )
            )
        ).all()
    return [_entry_dict(row) for row in rows]


async def _replace_table(keys: list[dict[str, Any]]) -> None:
    async with await get_db() as db:
        await db.execute(delete(ControlApiKey))
        for key in keys:
            key_hash = key.get("key_hash")
            user_id = key.get("user_id")
            if not isinstance(key_hash, str) or not key_hash or not user_id:
                continue
            db.add(
                ControlApiKey(
                    id=str(key.get("id") or uuid.uuid4()),
                    key_hash=key_hash,
                    user_id=str(user_id),
                    name=str(key.get("name") or "default"),
                    scopes=[
                        value.strip()
                        for value in (key.get("scopes") or key.get("control_scopes") or [])
                        if isinstance(value, str) and value.strip()
                    ],
                    created_at=int(key.get("created_at") or time.time()),
                )
            )
        await db.commit()
    clear_api_key_cache()


async def save_api_keys(keys: list[dict[str, Any]]) -> None:
    """Persist indexed keys while retaining the legacy Config mirror for compatibility."""
    await _replace_table(keys)
    await Config.upsert({"api_keys": keys})


async def resolve_api_key_principal(token_hash: str) -> ApiKeyPrincipal | None:
    now = time.monotonic()
    if CONTROL_AUTH_CACHE_TTL_SECONDS > 0:
        cached = _principal_cache.get(token_hash)
        if cached and cached[0] > now:
            _principal_cache.move_to_end(token_hash)
            return cached[1]
        if cached:
            _principal_cache.pop(token_hash, None)

    await migrate_legacy_api_keys()
    async with await get_db() as db:
        result = await db.execute(
            select(ControlApiKey, Auth.username)
            .outerjoin(Auth, Auth.user_id == ControlApiKey.user_id)
            .where(ControlApiKey.key_hash == token_hash)
            .limit(1)
        )
        row = result.first()
    if row is None:
        return None
    key, username = row
    principal = ApiKeyPrincipal(
        user_id=str(key.user_id),
        username=str(username) if username is not None else None,
        scopes=frozenset(
            value.strip()
            for value in (key.scopes or [])
            if isinstance(value, str) and value.strip()
        ),
    )
    if CONTROL_AUTH_CACHE_TTL_SECONDS > 0:
        _principal_cache[token_hash] = (
            now + CONTROL_AUTH_CACHE_TTL_SECONDS,
            principal,
        )
        _principal_cache.move_to_end(token_hash)
        while len(_principal_cache) > CONTROL_AUTH_CACHE_MAX_ENTRIES:
            _principal_cache.popitem(last=False)
    return principal


def api_key_cache_stats() -> dict[str, int]:
    return {"entries": len(_principal_cache), "max_entries": CONTROL_AUTH_CACHE_MAX_ENTRIES}
