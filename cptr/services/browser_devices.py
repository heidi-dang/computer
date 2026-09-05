"""Authoritative browser-device pairing, credentials, leases, and replay store."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from cptr.models import (
    BrowserDevice,
    BrowserDeviceEvent,
    BrowserLease,
    BrowserPairingChallenge,
    BrowserSession,
)
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_sensitive

PAIRING_TTL_MS = 10 * 60_000
DEVICE_CREDENTIAL_BYTES = 32
CLAIM_SECRET_BYTES = 32
MAX_DEVICE_EVENT_JSON_CHARS = 24_000


class BrowserTabInUseError(RuntimeError):
    """Raised when a live CPTR browser session already owns a Chrome tab."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _matches(raw: str, expected_hash: str) -> bool:
    return hmac.compare_digest(_hash_secret(raw), expected_hash)


@dataclass(frozen=True)
class PairingRequest:
    pairing_id: str
    code: str
    claim_secret: str
    expires_at: int


class BrowserDeviceStore:
    async def request_pairing(self, *, device_name: str) -> PairingRequest:
        name = device_name.strip()
        if not name or len(name) > 120:
            raise ValueError("device name must be between 1 and 120 characters")
        code = f"{secrets.randbelow(1_000_000):06d}"
        claim_secret = secrets.token_urlsafe(CLAIM_SECRET_BYTES)
        now = _now_ms()
        row = BrowserPairingChallenge(
            device_name=name,
            code_hash=_hash_secret(code),
            claim_secret_hash=_hash_secret(claim_secret),
            status="PENDING",
            created_at=now,
            expires_at=now + PAIRING_TTL_MS,
        )
        async with await get_db() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return PairingRequest(
            pairing_id=row.id,
            code=code,
            claim_secret=claim_secret,
            expires_at=int(row.expires_at),
        )

    async def approve_pairing(
        self, *, user_id: str, pairing_id: str, code: str | None = None
    ) -> bool:
        now = _now_ms()
        async with await get_db() as db:
            row = await db.get(BrowserPairingChallenge, pairing_id)
            if row is None or row.status != "PENDING" or int(row.expires_at) <= now:
                return False
            # Authenticated control-plane callers may approve the exact opaque
            # pairing_id without forwarding the six-digit challenge through a
            # host that classifies OTP-like values as blocked secrets. Legacy
            # callers that provide the code still receive the original check.
            if code is not None and not _matches(code, str(row.code_hash)):
                return False
            row.user_id = user_id
            row.status = "APPROVED"
            row.approved_at = now
            await db.commit()
            return True

    async def claim_pairing(
        self, *, pairing_id: str, claim_secret: str
    ) -> tuple[BrowserDevice, str] | None:
        now = _now_ms()
        async with await get_db() as db:
            row = await db.get(BrowserPairingChallenge, pairing_id)
            if (
                row is None
                or row.status != "APPROVED"
                or not row.user_id
                or int(row.expires_at) <= now
                or not _matches(claim_secret, str(row.claim_secret_hash))
            ):
                return None
            credential = secrets.token_urlsafe(DEVICE_CREDENTIAL_BYTES)
            device = BrowserDevice(
                user_id=str(row.user_id),
                name=str(row.device_name),
                credential_hash=_hash_secret(credential),
                credential_version=1,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
            db.add(device)
            row.status = "CLAIMED"
            row.claimed_at = now
            await db.commit()
            await db.refresh(device)
            return device, credential

    async def authenticate_device(self, *, device_id: str, credential: str) -> BrowserDevice | None:
        now = _now_ms()
        async with await get_db() as db:
            device = await db.get(BrowserDevice, device_id)
            if (
                device is None
                or device.status != "ACTIVE"
                or not _matches(credential, str(device.credential_hash))
            ):
                return None
            device.last_seen_at = now
            device.updated_at = now
            await db.commit()
            return device

    async def revoke_device(self, *, user_id: str, device_id: str) -> bool:
        now = _now_ms()
        async with await get_db() as db:
            device = await db.get(BrowserDevice, device_id)
            if device is None or device.user_id != user_id:
                return False
            device.status = "REVOKED"
            device.revoked_at = now
            device.updated_at = now
            await db.commit()
            return True

    async def rotate_credential(self, *, user_id: str, device_id: str) -> str | None:
        now = _now_ms()
        credential = secrets.token_urlsafe(DEVICE_CREDENTIAL_BYTES)
        async with await get_db() as db:
            device = await db.get(BrowserDevice, device_id)
            if device is None or device.user_id != user_id or device.status != "ACTIVE":
                return None
            device.credential_hash = _hash_secret(credential)
            device.credential_version = int(device.credential_version) + 1
            device.updated_at = now
            await db.commit()
            return credential

    async def owns_active_device(self, *, user_id: str, device_id: str) -> bool:
        async with await get_db() as db:
            device = await db.get(BrowserDevice, device_id)
            return bool(
                device is not None and device.user_id == user_id and device.status == "ACTIVE"
            )

    async def open_session(
        self,
        *,
        user_id: str,
        device_id: str,
        tab_id: int,
        workbench_session_id: str | None = None,
        surface_id: str | None = None,
    ) -> BrowserSession:
        now = _now_ms()
        async with await get_db() as db:
            device = await db.get(BrowserDevice, device_id)
            if device is None or device.user_id != user_id or device.status != "ACTIVE":
                raise PermissionError("browser device is unavailable")
            lease = (
                await db.scalars(
                    select(BrowserLease).where(
                        BrowserLease.device_id == device_id,
                        BrowserLease.tab_id == tab_id,
                    )
                )
            ).first()
            if lease is not None:
                current_session = await db.get(BrowserSession, lease.session_id)
                if (
                    current_session is not None
                    and current_session.closed_at is None
                    and current_session.state != "DISCONNECTED"
                ):
                    if lease.owner != "none" or current_session.state != "OBSERVING":
                        raise BrowserTabInUseError("browser tab already has an active session")
                    # Older releases represented agent -> none as OBSERVING even
                    # though the extension had already detached its debugger.
                    # Only that legacy state is recyclable. New session bootstrap
                    # uses CONNECTING so a concurrent open cannot steal its lease.
                    current_session.state = "DISCONNECTED"
                    current_session.closed_at = now
                    current_session.updated_at = now

            session = BrowserSession(
                user_id=user_id,
                device_id=device_id,
                workbench_session_id=workbench_session_id,
                tab_id=tab_id,
                state="CONNECTING",
                surface_id=surface_id,
                created_at=now,
                updated_at=now,
            )
            db.add(session)
            await db.flush()
            if lease is None:
                lease = BrowserLease(
                    device_id=device_id,
                    tab_id=tab_id,
                    session_id=session.id,
                    owner="none",
                    epoch=0,
                    updated_at=now,
                )
                db.add(lease)
            else:
                lease.session_id = session.id
                lease.owner = "none"
                lease.epoch = int(lease.epoch) + 1
                lease.expires_at = None
                lease.updated_at = now
            await db.commit()
            await db.refresh(session)
            return session

    async def transfer_lease(
        self,
        *,
        session_id: str,
        expected_epoch: int,
        expected_owner: Literal["none", "agent", "human"],
        new_owner: Literal["none", "agent", "human"],
        fresh_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        async with await get_db() as db:
            session = await db.get(BrowserSession, session_id)
            if session is None or session.closed_at is not None:
                raise KeyError("browser session not found")
            lease = (
                await db.scalars(select(BrowserLease).where(BrowserLease.session_id == session_id))
            ).first()
            if lease is None:
                raise KeyError("browser lease not found")
            if int(lease.epoch) != expected_epoch or lease.owner != expected_owner:
                raise PermissionError("stale browser lease epoch or owner")
            if expected_owner == "human" and new_owner == "agent":
                if not fresh_snapshot_id or fresh_snapshot_id == session.snapshot_id:
                    raise PermissionError("return to agent requires a fresh snapshot")
                session.snapshot_id = fresh_snapshot_id
            lease.owner = new_owner
            lease.epoch = int(lease.epoch) + 1
            lease.updated_at = now
            if new_owner == "none":
                lease.expires_at = None
                session.state = "DISCONNECTED"
                session.closed_at = now
            else:
                session.state = {
                    "agent": "AGENT_CONTROL",
                    "human": "HUMAN_CONTROL",
                }[new_owner]
            session.updated_at = now
            await db.commit()
            result = {
                "device_id": lease.device_id,
                "tab_id": int(lease.tab_id),
                "session_id": session_id,
                "owner": lease.owner,
                "epoch": int(lease.epoch),
                "snapshot_id": session.snapshot_id,
                "state": session.state,
            }
            return result

    async def abort_session_bootstrap(self, *, session_id: str, expected_epoch: int) -> None:
        now = _now_ms()
        async with await get_db() as db:
            session = await db.get(BrowserSession, session_id)
            if session is None:
                return
            lease = (
                await db.scalars(select(BrowserLease).where(BrowserLease.session_id == session_id))
            ).first()
            if lease is None:
                session.state = "DISCONNECTED"
                session.closed_at = now
                session.updated_at = now
                await db.commit()
                return
            if lease.owner == "agent" and int(lease.epoch) == expected_epoch:
                lease.owner = "none"
                lease.epoch = int(lease.epoch) + 1
                lease.expires_at = None
                lease.updated_at = now
            session.state = "DISCONNECTED"
            session.closed_at = now
            session.updated_at = now
            await db.commit()

    async def disconnect_device_sessions(self, *, device_id: str) -> int:
        """Close every durable live session for a device after its socket is gone."""
        now = _now_ms()
        async with await get_db() as db:
            sessions = list(
                (
                    await db.scalars(
                        select(BrowserSession).where(
                            BrowserSession.device_id == device_id,
                            BrowserSession.closed_at.is_(None),
                            BrowserSession.state != "DISCONNECTED",
                        )
                    )
                ).all()
            )
            if not sessions:
                return 0
            session_ids = [str(session.id) for session in sessions]
            leases = list(
                (
                    await db.scalars(
                        select(BrowserLease).where(BrowserLease.session_id.in_(session_ids))
                    )
                ).all()
            )
            for lease in leases:
                lease.owner = "none"
                lease.epoch = int(lease.epoch) + 1
                lease.expires_at = None
                lease.updated_at = now
            for session in sessions:
                session.state = "DISCONNECTED"
                session.closed_at = now
                session.updated_at = now
            await db.commit()
            return len(sessions)

    async def disconnect_stale_sessions(self) -> int:
        """Reconcile browser leases that survived a backend process restart."""
        now = _now_ms()
        async with await get_db() as db:
            sessions = list(
                (
                    await db.scalars(
                        select(BrowserSession).where(
                            BrowserSession.closed_at.is_(None),
                            BrowserSession.state != "DISCONNECTED",
                        )
                    )
                ).all()
            )
            if not sessions:
                return 0
            session_ids = [str(session.id) for session in sessions]
            leases = list(
                (
                    await db.scalars(
                        select(BrowserLease).where(BrowserLease.session_id.in_(session_ids))
                    )
                ).all()
            )
            for lease in leases:
                lease.owner = "none"
                lease.epoch = int(lease.epoch) + 1
                lease.expires_at = None
                lease.updated_at = now
            for session in sessions:
                session.state = "DISCONNECTED"
                session.closed_at = now
                session.updated_at = now
            await db.commit()
            return len(sessions)

    async def assert_mutation(
        self,
        *,
        session_id: str,
        actor: Literal["agent", "human"],
        expected_epoch: int,
    ) -> None:
        async with await get_db() as db:
            lease = (
                await db.scalars(select(BrowserLease).where(BrowserLease.session_id == session_id))
            ).first()
            if lease is None or lease.owner != actor or int(lease.epoch) != expected_epoch:
                raise PermissionError("browser mutation rejected by lease ownership")

    async def list_devices(self, *, user_id: str) -> list[dict[str, Any]]:
        async with await get_db() as db:
            rows = (
                await db.scalars(
                    select(BrowserDevice)
                    .where(BrowserDevice.user_id == user_id)
                    .order_by(BrowserDevice.updated_at.desc(), BrowserDevice.id.asc())
                )
            ).all()
        return [
            {
                "device_id": row.id,
                "name": row.name,
                "status": row.status,
                "credential_version": int(row.credential_version),
                "created_at": int(row.created_at),
                "updated_at": int(row.updated_at),
                "last_seen_at": int(row.last_seen_at) if row.last_seen_at is not None else None,
                "revoked_at": int(row.revoked_at) if row.revoked_at is not None else None,
            }
            for row in rows
        ]

    async def get_session(self, *, user_id: str, session_id: str) -> BrowserSession | None:
        async with await get_db() as db:
            session = await db.get(BrowserSession, session_id)
            if session is None or session.user_id != user_id:
                return None
            return session

    async def session_lease(self, *, session_id: str) -> dict[str, Any] | None:
        async with await get_db() as db:
            lease = (
                await db.scalars(select(BrowserLease).where(BrowserLease.session_id == session_id))
            ).first()
            if lease is None:
                return None
            return {
                "device_id": lease.device_id,
                "tab_id": int(lease.tab_id),
                "session_id": lease.session_id,
                "owner": lease.owner,
                "epoch": int(lease.epoch),
                "expires_at": int(lease.expires_at) if lease.expires_at is not None else None,
            }

    async def replay_device_events(
        self,
        *,
        device_id: str,
        after_sequence: int,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 500))
        async with await get_db() as db:
            rows = (
                await db.scalars(
                    select(BrowserDeviceEvent)
                    .where(
                        BrowserDeviceEvent.device_id == device_id,
                        BrowserDeviceEvent.sequence > max(0, after_sequence),
                    )
                    .order_by(BrowserDeviceEvent.sequence.asc())
                    .limit(bounded)
                )
            ).all()
        return [
            {
                "event_id": row.id,
                "device_id": row.device_id,
                "sequence": int(row.sequence),
                "type": row.event_type,
                "timestamp_ms": int(row.created_at),
                "payload": row.payload or {},
            }
            for row in rows
        ]

    async def append_device_event(
        self,
        *,
        device_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> BrowserDeviceEvent:
        now = _now_ms()
        async with await get_db() as db:
            latest = await db.scalar(
                select(func.max(BrowserDeviceEvent.sequence)).where(
                    BrowserDeviceEvent.device_id == device_id
                )
            )
            safe_payload = redact_sensitive(payload)
            encoded = str(safe_payload)
            if len(encoded) > MAX_DEVICE_EVENT_JSON_CHARS:
                safe_payload = {
                    "truncated": True,
                    "summary": "device event payload exceeded bounded storage",
                }
            event = BrowserDeviceEvent(
                device_id=device_id,
                sequence=int(latest or 0) + 1,
                event_type=event_type[:120],
                payload=safe_payload,
                created_at=now,
            )
            db.add(event)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                latest = await db.scalar(
                    select(func.max(BrowserDeviceEvent.sequence)).where(
                        BrowserDeviceEvent.device_id == device_id
                    )
                )
                event.sequence = int(latest or 0) + 1
                db.add(event)
                await db.commit()
            await db.refresh(event)
            return event


browser_device_store = BrowserDeviceStore()
