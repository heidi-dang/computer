"""Durable SQL persistence for Capability OS control-plane facts."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import (
    CapabilityOsArtifact,
    CapabilityOsEvidence,
    CapabilityOsLease,
    CapabilityOsMcpMount,
)
from cptr.services.capability_os.contracts import CptrArtifact, digest_payload
from cptr.utils.db import get_session_factory


def _now_ms() -> int:
    return int(time.time() * 1000)


def _created_at_ms(value: str) -> int:
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _now_ms()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


class SqlCapabilityOsStore:
    def __init__(self, *, session_factory: async_sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def persist_artifact(self, artifact: CptrArtifact[Any]) -> CapabilityOsArtifact:
        async with self._session_factory() as db:
            existing = await db.scalar(
                select(CapabilityOsArtifact).where(
                    CapabilityOsArtifact.content_digest == artifact.metadata.content_digest
                )
            )
            if existing is not None:
                if (
                    existing.artifact_id != artifact.metadata.id
                    or existing.version != artifact.metadata.version
                    or existing.kind != artifact.kind.value
                ):
                    raise ValueError("artifact digest collision with different immutable identity")
                return existing
            row = CapabilityOsArtifact(
                artifact_id=artifact.metadata.id,
                version=artifact.metadata.version,
                kind=artifact.kind.value,
                owner=artifact.metadata.owner.value,
                user_id=artifact.metadata.user_id,
                origin=artifact.metadata.origin.value,
                state=artifact.state.value,
                parent_ref=artifact.metadata.parent,
                task_origin=artifact.metadata.task_origin,
                source_digest=artifact.metadata.source_digest,
                content_digest=artifact.metadata.content_digest,
                compatibility=artifact.compatibility,
                spec=artifact.spec,
                created_at=artifact.metadata.created_at,
                created_at_ms=_created_at_ms(artifact.metadata.created_at),
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row

    async def list_artifacts(
        self,
        *,
        task_id: str | None = None,
        user_id: str | None = None,
        include_global: bool = True,
        kinds: tuple[str, ...] = (),
        states: tuple[str, ...] = (),
        limit: int = 200,
    ) -> list[CapabilityOsArtifact]:
        limit = max(1, min(int(limit), 1000))
        async with self._session_factory() as db:
            query = select(CapabilityOsArtifact)
            if task_id is not None:
                query = query.where(CapabilityOsArtifact.task_origin == task_id)
            if user_id is not None:
                if include_global:
                    query = query.where(
                        (CapabilityOsArtifact.user_id == user_id)
                        | CapabilityOsArtifact.user_id.is_(None)
                    )
                else:
                    query = query.where(CapabilityOsArtifact.user_id == user_id)
            if kinds:
                query = query.where(CapabilityOsArtifact.kind.in_(tuple(kinds)))
            if states:
                query = query.where(CapabilityOsArtifact.state.in_(tuple(states)))
            return list(
                (
                    await db.scalars(
                        query.order_by(
                            CapabilityOsArtifact.created_at_ms.desc(),
                            CapabilityOsArtifact.id.desc(),
                        ).limit(limit)
                    )
                ).all()
            )

    async def get_artifact(
        self,
        content_digest: str,
        *,
        user_id: str | None = None,
        include_global: bool = True,
    ) -> CapabilityOsArtifact | None:
        async with self._session_factory() as db:
            query = select(CapabilityOsArtifact).where(
                CapabilityOsArtifact.content_digest == content_digest
            )
            if user_id is not None:
                if include_global:
                    query = query.where(
                        (CapabilityOsArtifact.user_id == user_id)
                        | CapabilityOsArtifact.user_id.is_(None)
                    )
                else:
                    query = query.where(CapabilityOsArtifact.user_id == user_id)
            return await db.scalar(query)

    async def set_artifact_state(
        self,
        content_digest: str,
        *,
        state: str,
        retired_at_ms: int | None = None,
    ) -> bool:
        async with self._session_factory() as db:
            result = await db.execute(
                update(CapabilityOsArtifact)
                .where(CapabilityOsArtifact.content_digest == content_digest)
                .values(state=state, retired_at_ms=retired_at_ms)
            )
            await db.commit()
            return bool(result.rowcount)

    async def compare_and_set_artifact_state(
        self,
        content_digest: str,
        *,
        expected_state: str,
        state: str,
    ) -> bool:
        async with self._session_factory() as db:
            result = await db.execute(
                update(CapabilityOsArtifact)
                .where(
                    CapabilityOsArtifact.content_digest == content_digest,
                    CapabilityOsArtifact.state == expected_state,
                )
                .values(state=state)
            )
            await db.commit()
            return bool(result.rowcount)

    async def artifact_exists(self, content_digest: str) -> bool:
        async with self._session_factory() as db:
            return (
                await db.scalar(
                    select(CapabilityOsArtifact.id).where(
                        CapabilityOsArtifact.content_digest == content_digest
                    )
                )
            ) is not None

    async def create_lease(
        self,
        *,
        task_id: str,
        workload_id: str,
        artifact_digest: str,
        permissions: list[dict[str, Any]],
        resource_limits: dict[str, Any],
        network: dict[str, Any],
        credentials: dict[str, Any],
        approval_id: str | None,
        parent_lease_id: str | None,
        policy_decision_id: str,
        attestation_requirements: dict[str, Any],
        runtime_profile: str,
        issued_at_ms: int,
        expires_at_ms: int,
    ) -> CapabilityOsLease:
        row = CapabilityOsLease(
            task_id=task_id,
            workload_id=workload_id,
            artifact_digest=artifact_digest,
            permissions=permissions,
            resource_limits=resource_limits,
            network=network,
            credentials=credentials,
            approval_id=approval_id,
            parent_lease_id=parent_lease_id,
            policy_decision_id=policy_decision_id,
            attestation_requirements=attestation_requirements,
            runtime_profile=runtime_profile,
            status="active",
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
        )
        async with self._session_factory() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row

    async def get_lease(self, lease_id: str) -> CapabilityOsLease | None:
        async with self._session_factory() as db:
            return await db.get(CapabilityOsLease, lease_id)

    async def list_active_leases(self, task_id: str, *, now_ms: int) -> list[CapabilityOsLease]:
        async with self._session_factory() as db:
            rows = list(
                (
                    await db.scalars(
                        select(CapabilityOsLease)
                        .where(
                            CapabilityOsLease.task_id == task_id,
                            CapabilityOsLease.status == "active",
                            CapabilityOsLease.expires_at_ms > int(now_ms),
                        )
                        .order_by(CapabilityOsLease.issued_at_ms, CapabilityOsLease.lease_id)
                    )
                ).all()
            )
        return rows

    async def revoke_lease(self, lease_id: str, *, now_ms: int) -> bool:
        async with self._session_factory() as db:
            result = await db.execute(
                update(CapabilityOsLease)
                .where(
                    CapabilityOsLease.lease_id == lease_id,
                    CapabilityOsLease.status == "active",
                )
                .values(status="revoked", revoked_at_ms=int(now_ms))
            )
            await db.commit()
            return bool(result.rowcount)

    async def revoke_task_leases(self, task_id: str, *, now_ms: int) -> int:
        async with self._session_factory() as db:
            result = await db.execute(
                update(CapabilityOsLease)
                .where(
                    CapabilityOsLease.task_id == task_id,
                    CapabilityOsLease.status == "active",
                )
                .values(status="revoked", revoked_at_ms=int(now_ms))
            )
            await db.commit()
            return int(result.rowcount or 0)

    async def create_mcp_mount(
        self,
        *,
        task_id: str,
        server_id: str,
        version: str,
        digest: str,
        lease_id: str,
        projected_tools: tuple[str, ...],
        transport_kind: str,
        now_ms: int,
    ) -> CapabilityOsMcpMount:
        row = CapabilityOsMcpMount(
            task_id=task_id,
            server_id=server_id,
            version=version,
            digest=digest,
            state="mounted",
            lease_id=lease_id,
            projected_tools=list(projected_tools),
            transport_kind=transport_kind,
            created_at_ms=int(now_ms),
            updated_at_ms=int(now_ms),
        )
        async with self._session_factory() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row

    async def get_mcp_mount(self, mount_id: str) -> CapabilityOsMcpMount | None:
        async with self._session_factory() as db:
            return await db.get(CapabilityOsMcpMount, mount_id)

    async def release_mcp_mount(self, mount_id: str, *, now_ms: int) -> bool:
        async with self._session_factory() as db:
            result = await db.execute(
                update(CapabilityOsMcpMount)
                .where(
                    CapabilityOsMcpMount.mount_id == mount_id,
                    CapabilityOsMcpMount.state == "mounted",
                )
                .values(state="released", updated_at_ms=int(now_ms), released_at_ms=int(now_ms))
            )
            await db.commit()
            return bool(result.rowcount)

    async def release_task_mounts(self, task_id: str, *, now_ms: int) -> int:
        async with self._session_factory() as db:
            result = await db.execute(
                update(CapabilityOsMcpMount)
                .where(
                    CapabilityOsMcpMount.task_id == task_id,
                    CapabilityOsMcpMount.state == "mounted",
                )
                .values(state="released", updated_at_ms=int(now_ms), released_at_ms=int(now_ms))
            )
            await db.commit()
            return int(result.rowcount or 0)

    async def list_active_mounts(self, task_id: str) -> list[CapabilityOsMcpMount]:
        async with self._session_factory() as db:
            return list(
                (
                    await db.scalars(
                        select(CapabilityOsMcpMount)
                        .where(
                            CapabilityOsMcpMount.task_id == task_id,
                            CapabilityOsMcpMount.state == "mounted",
                        )
                        .order_by(CapabilityOsMcpMount.created_at_ms, CapabilityOsMcpMount.mount_id)
                    )
                ).all()
            )

    async def append_evidence(
        self,
        *,
        task_id: str,
        kind: str,
        producer_identity: str,
        claims: dict[str, Any],
        run_id: str | None = None,
        blob_uri: str | None = None,
        artifact_digest: str | None = None,
        lease_id: str | None = None,
        created_at_ms: int | None = None,
    ) -> CapabilityOsEvidence:
        safe_claims = dict(claims)
        timestamp = int(created_at_ms if created_at_ms is not None else _now_ms())
        # The task+sequence uniqueness constraint turns concurrent head races
        # into a failed append rather than an undetectable evidence-chain fork.
        for _attempt in range(3):
            async with self._session_factory() as db:
                previous = await db.scalar(
                    select(CapabilityOsEvidence)
                    .where(CapabilityOsEvidence.task_id == task_id)
                    .order_by(
                        CapabilityOsEvidence.sequence.desc(),
                        CapabilityOsEvidence.evidence_id.desc(),
                    )
                    .limit(1)
                )
                sequence = int(previous.sequence) + 1 if previous is not None else 1
                previous_digest = previous.digest if previous is not None else None
                digest = digest_payload(
                    {
                        "task_id": task_id,
                        "sequence": sequence,
                        "previous_digest": previous_digest,
                        "run_id": run_id,
                        "kind": kind,
                        "producer_identity": producer_identity,
                        "claims": safe_claims,
                        "blob_uri": blob_uri,
                        "artifact_digest": artifact_digest,
                        "lease_id": lease_id,
                        "created_at_ms": timestamp,
                    }
                )
                row = CapabilityOsEvidence(
                    task_id=task_id,
                    sequence=sequence,
                    previous_digest=previous_digest,
                    run_id=run_id,
                    kind=kind,
                    producer_identity=producer_identity,
                    claims=safe_claims,
                    blob_uri=blob_uri,
                    digest=digest,
                    artifact_digest=artifact_digest,
                    lease_id=lease_id,
                    created_at_ms=timestamp,
                )
                db.add(row)
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    continue
                await db.refresh(row)
                return row
        raise RuntimeError("evidence append could not serialize task chain head")

    async def get_evidence(self, evidence_id: str) -> CapabilityOsEvidence | None:
        async with self._session_factory() as db:
            return await db.get(CapabilityOsEvidence, evidence_id)

    async def list_evidence(self, task_id: str, *, limit: int = 100) -> list[CapabilityOsEvidence]:
        limit = max(1, min(int(limit), 500))
        async with self._session_factory() as db:
            return list(
                (
                    await db.scalars(
                        select(CapabilityOsEvidence)
                        .where(CapabilityOsEvidence.task_id == task_id)
                        .order_by(CapabilityOsEvidence.sequence, CapabilityOsEvidence.evidence_id)
                        .limit(limit)
                    )
                ).all()
            )

    async def list_evidence_for_run(
        self,
        task_id: str,
        run_id: str,
        *,
        limit: int = 500,
    ) -> list[CapabilityOsEvidence]:
        task_id = str(task_id).strip()
        run_id = str(run_id).strip()
        if not task_id or not run_id:
            raise ValueError("task_id and run_id must not be blank")
        limit = max(1, min(int(limit), 2500))
        async with self._session_factory() as db:
            return list(
                (
                    await db.scalars(
                        select(CapabilityOsEvidence)
                        .where(
                            CapabilityOsEvidence.task_id == task_id,
                            CapabilityOsEvidence.run_id == run_id,
                        )
                        .order_by(CapabilityOsEvidence.sequence, CapabilityOsEvidence.evidence_id)
                        .limit(limit)
                    )
                ).all()
            )

    async def list_artifact_evidence(
        self,
        artifact_digest: str,
        *,
        limit: int = 200,
    ) -> list[CapabilityOsEvidence]:
        artifact_digest = str(artifact_digest).strip()
        if not artifact_digest:
            raise ValueError("artifact_digest must not be blank")
        limit = max(1, min(int(limit), 1000))
        async with self._session_factory() as db:
            return list(
                (
                    await db.scalars(
                        select(CapabilityOsEvidence)
                        .where(CapabilityOsEvidence.artifact_digest == artifact_digest)
                        .order_by(
                            CapabilityOsEvidence.created_at_ms.desc(),
                            CapabilityOsEvidence.evidence_id.desc(),
                        )
                        .limit(limit)
                    )
                ).all()
            )

    async def verify_evidence_chain(self, task_id: str) -> dict[str, Any]:
        async with self._session_factory() as db:
            rows = list(
                (
                    await db.scalars(
                        select(CapabilityOsEvidence)
                        .where(CapabilityOsEvidence.task_id == task_id)
                        .order_by(CapabilityOsEvidence.sequence, CapabilityOsEvidence.evidence_id)
                    )
                ).all()
            )
        previous_digest: str | None = None
        expected_sequence = 1
        for row in rows:
            if int(row.sequence) != expected_sequence:
                return {
                    "valid": False,
                    "count": len(rows),
                    "headDigest": previous_digest,
                    "error": "sequence-gap",
                    "errorSequence": expected_sequence,
                }
            if row.previous_digest != previous_digest:
                return {
                    "valid": False,
                    "count": len(rows),
                    "headDigest": previous_digest,
                    "error": "previous-digest-mismatch",
                    "errorSequence": expected_sequence,
                }
            expected_digest = digest_payload(
                {
                    "task_id": row.task_id,
                    "sequence": int(row.sequence),
                    "previous_digest": row.previous_digest,
                    "run_id": row.run_id,
                    "kind": row.kind,
                    "producer_identity": row.producer_identity,
                    "claims": dict(row.claims or {}),
                    "blob_uri": row.blob_uri,
                    "artifact_digest": row.artifact_digest,
                    "lease_id": row.lease_id,
                    "created_at_ms": int(row.created_at_ms),
                }
            )
            if expected_digest != row.digest:
                return {
                    "valid": False,
                    "count": len(rows),
                    "headDigest": previous_digest,
                    "error": "digest-mismatch",
                    "errorSequence": expected_sequence,
                }
            previous_digest = row.digest
            expected_sequence += 1
        return {
            "valid": True,
            "count": len(rows),
            "headDigest": previous_digest,
            "error": None,
            "errorSequence": None,
        }
