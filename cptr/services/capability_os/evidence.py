"""Tamper-evident, redacted Capability OS evidence recording boundary."""

from __future__ import annotations

from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.utils.redaction import redact_sensitive


class EvidenceViolation(ValueError):
    pass


class EvidenceService:
    def __init__(self, *, store: SqlCapabilityOsStore, clock_ms) -> None:
        self._store = store
        self._clock_ms = clock_ms

    async def record(
        self,
        *,
        task_id: str,
        kind: str,
        producer_identity: str,
        claims: dict,
        run_id: str | None = None,
        blob_uri: str | None = None,
        artifact_digest: str | None = None,
        lease_id: str | None = None,
    ):
        if not task_id.strip() or not kind.strip() or not producer_identity.strip():
            raise EvidenceViolation("evidence task, kind, and producer must not be blank")
        if not isinstance(claims, dict):
            raise EvidenceViolation("evidence claims must be an object")
        now_ms = int(self._clock_ms())

        if artifact_digest is not None and not await self._store.artifact_exists(artifact_digest):
            raise EvidenceViolation("evidence references an unknown artifact")
        if lease_id is not None:
            lease = await self._store.get_lease(lease_id)
            if lease is None:
                raise EvidenceViolation("evidence references an unknown lease")
            if lease.status != "active" or int(lease.expires_at_ms) <= now_ms:
                raise EvidenceViolation("evidence references an inactive lease")
            if lease.task_id != task_id:
                raise EvidenceViolation("evidence lease belongs to a different task")
            if artifact_digest is not None and lease.artifact_digest != artifact_digest:
                raise EvidenceViolation("evidence artifact does not match lease subject")

        safe_claims = redact_sensitive(claims)
        return await self._store.append_evidence(
            task_id=task_id,
            run_id=run_id,
            kind=kind,
            producer_identity=producer_identity,
            claims=safe_claims,
            blob_uri=blob_uri,
            artifact_digest=artifact_digest,
            lease_id=lease_id,
            created_at_ms=now_ms,
        )

    async def verify_chain(self, task_id: str) -> dict:
        if not str(task_id).strip():
            raise EvidenceViolation("evidence task must not be blank")
        return await self._store.verify_evidence_chain(task_id)
