"""Bounded Workbench-scoped ADMIN grants.

ADMIN is an owner-scoped, expiring Workbench capability. It is intentionally
separate from LocalRootGrant and does not bypass command, network, deployment,
credential, history, or destructive-operation guards.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select

from cptr.models import AdminSessionGrant, AdminSessionGrantEvent, WorkbenchSession
from cptr.utils.db import get_db

DEFAULT_ADMIN_TTL_SECONDS = 15 * 60
MAX_ADMIN_TTL_SECONDS = 8 * 60 * 60


class AdminSessionGrantDenied(PermissionError):
    """Raised when an ADMIN grant cannot be issued or used."""


class PrivilegeBrokerDenied(PermissionError):
    """Raised when an operation requires privilege the caller does not hold."""


def _now_ms() -> int:
    return int(time.time() * 1000)


class AdminSessionGrantStore:
    async def _session(
        self,
        db: Any,
        *,
        owner_id: str,
        session_id: str,
        require_active: bool,
    ) -> WorkbenchSession | None:
        stmt = select(WorkbenchSession).where(
            WorkbenchSession.id == session_id,
            WorkbenchSession.user_id == owner_id,
            WorkbenchSession.deleted_at.is_(None),
        )
        if require_active:
            stmt = stmt.where(WorkbenchSession.archived_at.is_(None))
        return await db.scalar(stmt)

    @staticmethod
    def _event(
        *,
        owner_id: str,
        session_id: str,
        action: str,
        details: dict[str, Any],
        created_at: int,
    ) -> AdminSessionGrantEvent:
        return AdminSessionGrantEvent(
            workbench_session_id=session_id,
            user_id=owner_id,
            action=action,
            details=details,
            created_at=created_at,
        )

    async def _append_workbench_event(
        self,
        *,
        owner_id: str,
        session_id: str,
        action: str,
        details: dict[str, Any],
    ) -> None:
        try:
            from cptr.services.workbench_sessions import workbench_session_store

            await workbench_session_store.append_event(
                owner_id=owner_id,
                session_id=session_id,
                source="privilege_broker",
                actor=str(details.get("actor") or "user"),
                event_type=f"workbench.privilege.admin_{action}",
                state=None,
                summary=f"Workbench ADMIN grant {action}.",
                details=details,
                policy={"privilege": "ADMIN", "authority": "admin_session_grant"},
            )
        except Exception:
            # The authoritative audit record is committed with the grant row.
            pass

    async def grant(
        self,
        *,
        owner_id: str,
        session_id: str,
        ttl_seconds: int | None = None,
        now_ms: int | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        ttl = DEFAULT_ADMIN_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
        if ttl <= 0:
            raise ValueError("admin grant TTL must be positive")
        if ttl > MAX_ADMIN_TTL_SECONDS:
            raise ValueError(f"admin grant TTL exceeds maximum of {MAX_ADMIN_TTL_SECONDS} seconds")

        current = _now_ms() if now_ms is None else int(now_ms)
        expires_at = current + ttl * 1000
        replaced_count = 0

        async with await get_db() as db:
            session = await self._session(
                db,
                owner_id=owner_id,
                session_id=session_id,
                require_active=True,
            )
            if session is None:
                raise AdminSessionGrantDenied(
                    "admin grant requires an active owned Workbench session"
                )

            rows = list(
                (
                    await db.scalars(
                        select(AdminSessionGrant).where(
                            AdminSessionGrant.workbench_session_id == session_id,
                            AdminSessionGrant.user_id == owner_id,
                            AdminSessionGrant.revoked_at.is_(None),
                        )
                    )
                ).all()
            )
            for row in rows:
                row.revoked_at = current
                replaced_count += 1

            grant = AdminSessionGrant(
                workbench_session_id=session_id,
                user_id=owner_id,
                granted_at=current,
                expires_at=expires_at,
                revoked_at=None,
            )
            db.add(grant)
            await db.flush()

            details = {
                "grant_id": str(grant.id),
                "expires_at": expires_at,
                "ttl_seconds": ttl,
                "replaced_count": replaced_count,
                "actor": actor,
            }
            db.add(
                self._event(
                    owner_id=owner_id,
                    session_id=session_id,
                    action="granted",
                    details=details,
                    created_at=current,
                )
            )
            session.admin_role = "ADMIN"
            session.role_context = {
                "authority": "admin_session_grant",
                "grant_id": str(grant.id),
                "expires_at": expires_at,
            }
            session.updated_at = current
            await db.commit()

        await self._append_workbench_event(
            owner_id=owner_id,
            session_id=session_id,
            action="granted",
            details=details,
        )
        return {
            "grant_id": details["grant_id"],
            "workbench_session_id": session_id,
            "granted_at": current,
            "expires_at": expires_at,
            "ttl_seconds": ttl,
        }

    async def revoke(
        self,
        *,
        owner_id: str,
        session_id: str,
        now_ms: int | None = None,
        actor: str = "user",
    ) -> int:
        current = _now_ms() if now_ms is None else int(now_ms)
        revoked_count = 0
        details: dict[str, Any] | None = None

        async with await get_db() as db:
            session = await self._session(
                db,
                owner_id=owner_id,
                session_id=session_id,
                require_active=False,
            )
            if session is None:
                raise AdminSessionGrantDenied("owned Workbench session not found")

            rows = list(
                (
                    await db.scalars(
                        select(AdminSessionGrant).where(
                            AdminSessionGrant.workbench_session_id == session_id,
                            AdminSessionGrant.user_id == owner_id,
                            AdminSessionGrant.revoked_at.is_(None),
                        )
                    )
                ).all()
            )
            for row in rows:
                row.revoked_at = current
                revoked_count += 1

            if revoked_count:
                details = {"revoked_count": revoked_count, "actor": actor}
                db.add(
                    self._event(
                        owner_id=owner_id,
                        session_id=session_id,
                        action="revoked",
                        details=details,
                        created_at=current,
                    )
                )
            if str(session.admin_role or "").upper() == "ADMIN":
                session.admin_role = None
                session.role_context = None
                session.updated_at = current
            await db.commit()

        if details is not None:
            await self._append_workbench_event(
                owner_id=owner_id,
                session_id=session_id,
                action="revoked",
                details=details,
            )
        return revoked_count

    async def status(
        self,
        *,
        owner_id: str,
        session_id: str,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        current = _now_ms() if now_ms is None else int(now_ms)
        expired_details: dict[str, Any] | None = None

        async with await get_db() as db:
            session = await self._session(
                db,
                owner_id=owner_id,
                session_id=session_id,
                require_active=True,
            )
            if session is None:
                # An archived Workbench must not retain reusable ADMIN authority.
                owner_session = await self._session(
                    db,
                    owner_id=owner_id,
                    session_id=session_id,
                    require_active=False,
                )
                if owner_session is not None:
                    rows = list(
                        (
                            await db.scalars(
                                select(AdminSessionGrant).where(
                                    AdminSessionGrant.workbench_session_id == session_id,
                                    AdminSessionGrant.user_id == owner_id,
                                    AdminSessionGrant.revoked_at.is_(None),
                                )
                            )
                        ).all()
                    )
                    for row in rows:
                        row.revoked_at = current
                    projection_cleared = False
                    if str(owner_session.admin_role or "").upper() == "ADMIN":
                        owner_session.admin_role = None
                        owner_session.role_context = None
                        owner_session.updated_at = current
                        projection_cleared = True
                    if rows:
                        db.add(
                            self._event(
                                owner_id=owner_id,
                                session_id=session_id,
                                action="revoked_inactive_session",
                                details={"revoked_count": len(rows), "actor": "runtime"},
                                created_at=current,
                            )
                        )
                    if rows or projection_cleared:
                        await db.commit()
                return {
                    "active": False,
                    "grant_id": None,
                    "expires_at": None,
                    "remaining_seconds": 0,
                }

            row = await db.scalar(
                select(AdminSessionGrant)
                .where(
                    AdminSessionGrant.workbench_session_id == session_id,
                    AdminSessionGrant.user_id == owner_id,
                    AdminSessionGrant.revoked_at.is_(None),
                )
                .order_by(AdminSessionGrant.granted_at.desc())
                .limit(1)
            )
            if row is None:
                if str(session.admin_role or "").upper() == "ADMIN":
                    session.admin_role = None
                    session.role_context = None
                    session.updated_at = current
                    await db.commit()
                return {
                    "active": False,
                    "grant_id": None,
                    "expires_at": None,
                    "remaining_seconds": 0,
                }

            expires_at = int(row.expires_at)
            if expires_at <= current:
                row.revoked_at = current
                session.admin_role = None
                session.role_context = None
                session.updated_at = current
                expired_details = {
                    "grant_id": str(row.id),
                    "expires_at": expires_at,
                    "actor": "runtime",
                }
                db.add(
                    self._event(
                        owner_id=owner_id,
                        session_id=session_id,
                        action="expired",
                        details=expired_details,
                        created_at=current,
                    )
                )
                await db.commit()
                result = {
                    "active": False,
                    "grant_id": str(row.id),
                    "expires_at": expires_at,
                    "remaining_seconds": 0,
                }
            else:
                result = {
                    "active": True,
                    "grant_id": str(row.id),
                    "expires_at": expires_at,
                    "remaining_seconds": max(0, (expires_at - current) // 1000),
                }

        if expired_details is not None:
            await self._append_workbench_event(
                owner_id=owner_id,
                session_id=session_id,
                action="expired",
                details=expired_details,
            )
        return result

    async def is_active(
        self,
        *,
        owner_id: str,
        session_id: str,
        now_ms: int | None = None,
    ) -> bool:
        return bool(
            (
                await self.status(
                    owner_id=owner_id,
                    session_id=session_id,
                    now_ms=now_ms,
                )
            )["active"]
        )


class PrivilegeBroker:
    """Resolve effective Workbench privilege without widening guard policy."""

    def __init__(
        self,
        admin_store: AdminSessionGrantStore | None = None,
        root_store: Any | None = None,
    ) -> None:
        self.admin_store = admin_store or AdminSessionGrantStore()
        self._root_store = root_store

    @property
    def root_store(self) -> Any:
        if self._root_store is not None:
            return self._root_store
        from cptr.services.local_root_grants import local_root_grant_store

        return local_root_grant_store

    async def resolve_privilege(
        self,
        *,
        owner_id: str,
        session_id: str | None,
        now_ms: int | None = None,
    ) -> str:
        if not session_id:
            return "NORMAL"

        from cptr.services.local_root_grants import local_root_grants_enabled

        if local_root_grants_enabled() and await self.root_store.is_active(
            owner_id=owner_id,
            session_id=session_id,
            now_ms=now_ms,
        ):
            return "ROOT"
        if await self.admin_store.is_active(
            owner_id=owner_id,
            session_id=session_id,
            now_ms=now_ms,
        ):
            return "ADMIN"
        return "NORMAL"

    async def require_admin(
        self,
        *,
        owner_id: str,
        session_id: str | None,
    ) -> None:
        if not session_id or not await self.admin_store.is_active(
            owner_id=owner_id,
            session_id=session_id,
        ):
            raise PrivilegeBrokerDenied("an active Workbench ADMIN grant is required")


admin_session_grant_store = AdminSessionGrantStore()
privilege_broker = PrivilegeBroker(admin_session_grant_store)

__all__ = [
    "AdminSessionGrantDenied",
    "AdminSessionGrantStore",
    "DEFAULT_ADMIN_TTL_SECONDS",
    "MAX_ADMIN_TTL_SECONDS",
    "PrivilegeBroker",
    "PrivilegeBrokerDenied",
    "admin_session_grant_store",
    "privilege_broker",
]
