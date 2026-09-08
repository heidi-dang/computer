"""Explicit Workbench-scoped grants for unrestricted local root commands.

The grant is durable control-plane state. It is never inferred from a command,
model output, or caller identity claim: the caller must carry the exact
``# cptr-root: use root`` marker on an authenticated command that is bound to an
owned Workbench session. Once granted, the session remains root-enabled until
it is revoked, expires by an explicit user limit, or the Workbench session is
archived/deleted.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from sqlalchemy import select, update

from cptr.models import LocalRootGrant, WorkbenchSession
from cptr.utils.db import get_db

ROOT_GRANT_MARKER = "# cptr-root: use root"
ROOT_REVOKE_MARKER = "# cptr-root: revoke"
ROOT_TTL_PREFIX = "# cptr-root-ttl-seconds:"
LOCAL_ROOT_GRANTS_ENV = "CPTR_LOCAL_ROOT_GRANTS_ENABLED"
_MAX_EXPLICIT_TTL_SECONDS = 10 * 365 * 24 * 60 * 60


def local_root_grants_enabled() -> bool:
    return os.getenv(LOCAL_ROOT_GRANTS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


class LocalRootGrantDenied(PermissionError):
    pass


@dataclass(frozen=True)
class RootCommandDirective:
    command: str
    grant: bool = False
    revoke: bool = False
    ttl_seconds: int | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def parse_root_command_directive(command: str) -> RootCommandDirective:
    """Parse and strip the server-recognized root-control preamble.

    The marker must be the first logical line, so arbitrary occurrences inside a
    script or quoted string never change authority. An optional TTL line is only
    accepted immediately after the grant marker. A marker-only request executes
    ``:`` as a harmless shell no-op after changing grant state.
    """

    lines = command.splitlines(keepends=True)
    if not lines:
        return RootCommandDirective(command=command)
    first = lines[0].strip()
    if first == ROOT_REVOKE_MARKER:
        remaining = "".join(lines[1:]).lstrip("\r\n")
        return RootCommandDirective(command=remaining or ":", revoke=True)
    if first != ROOT_GRANT_MARKER:
        return RootCommandDirective(command=command)

    ttl_seconds: int | None = None
    consumed = 1
    if len(lines) > 1 and lines[1].strip().startswith(ROOT_TTL_PREFIX):
        raw = lines[1].strip()[len(ROOT_TTL_PREFIX) :].strip()
        if not re.fullmatch(r"[1-9][0-9]*", raw):
            raise ValueError("root grant TTL must be a positive integer number of seconds")
        ttl_seconds = int(raw)
        if ttl_seconds > _MAX_EXPLICIT_TTL_SECONDS:
            raise ValueError("root grant TTL exceeds the supported explicit limit")
        consumed = 2
    remaining = "".join(lines[consumed:]).lstrip("\r\n")
    return RootCommandDirective(command=remaining or ":", grant=True, ttl_seconds=ttl_seconds)


class LocalRootGrantStore:
    async def _active_session(self, *, owner_id: str, session_id: str) -> WorkbenchSession:
        async with await get_db() as db:
            session = await db.scalar(
                select(WorkbenchSession).where(
                    WorkbenchSession.id == session_id,
                    WorkbenchSession.user_id == owner_id,
                    WorkbenchSession.deleted_at.is_(None),
                    WorkbenchSession.archived_at.is_(None),
                )
            )
        if session is None:
            raise LocalRootGrantDenied("root grant requires an active owned Workbench session")
        return session

    async def grant(
        self,
        *,
        owner_id: str,
        session_id: str,
        ttl_seconds: int | None = None,
        now_ms: int | None = None,
    ) -> dict[str, int | str | None]:
        await self._active_session(owner_id=owner_id, session_id=session_id)
        current = _now_ms() if now_ms is None else int(now_ms)
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("root grant TTL must be positive")
        expires_at = current + int(ttl_seconds) * 1000 if ttl_seconds is not None else None
        async with await get_db() as db:
            await db.execute(
                update(LocalRootGrant)
                .where(
                    LocalRootGrant.workbench_session_id == session_id,
                    LocalRootGrant.user_id == owner_id,
                    LocalRootGrant.revoked_at.is_(None),
                )
                .values(revoked_at=current)
            )
            row = LocalRootGrant(
                workbench_session_id=session_id,
                user_id=owner_id,
                granted_at=current,
                expires_at=expires_at,
                revoked_at=None,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return {
                "grant_id": row.id,
                "workbench_session_id": session_id,
                "granted_at": current,
                "expires_at": expires_at,
            }

    async def revoke(
        self,
        *,
        owner_id: str,
        session_id: str,
        now_ms: int | None = None,
    ) -> int:
        current = _now_ms() if now_ms is None else int(now_ms)
        async with await get_db() as db:
            result = await db.execute(
                update(LocalRootGrant)
                .where(
                    LocalRootGrant.workbench_session_id == session_id,
                    LocalRootGrant.user_id == owner_id,
                    LocalRootGrant.revoked_at.is_(None),
                )
                .values(revoked_at=current)
            )
            await db.commit()
            return int(result.rowcount or 0)

    async def is_active(
        self,
        *,
        owner_id: str,
        session_id: str,
        now_ms: int | None = None,
    ) -> bool:
        current = _now_ms() if now_ms is None else int(now_ms)
        try:
            await self._active_session(owner_id=owner_id, session_id=session_id)
        except LocalRootGrantDenied:
            return False
        async with await get_db() as db:
            row = await db.scalar(
                select(LocalRootGrant)
                .where(
                    LocalRootGrant.workbench_session_id == session_id,
                    LocalRootGrant.user_id == owner_id,
                    LocalRootGrant.revoked_at.is_(None),
                )
                .order_by(LocalRootGrant.granted_at.desc())
                .limit(1)
            )
            if row is None:
                return False
            if row.expires_at is not None and int(row.expires_at) <= current:
                row.revoked_at = current
                await db.commit()
                return False
            return True


local_root_grant_store = LocalRootGrantStore()
