import unittest

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, CapabilityOsEvidence
from cptr.services.capability_os.authority import AuthorityBroker, LeaseRequest, TaskAuthorityPolicy
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    CapabilityRequest,
    create_artifact,
)
from cptr.services.capability_os.evidence import EvidenceService, EvidenceViolation
from cptr.services.capability_os.store import SqlCapabilityOsStore


class CapabilityOsEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=sessions)
        artifact = create_artifact(
            artifact_id="tool.evidence",
            version="1",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec={"entrypoint": "main.py"},
            created_at="2026-09-07T01:00:00Z",
            task_origin="task-1",
        )
        await self.store.persist_artifact(artifact)
        self.artifact_digest = artifact.metadata.content_digest
        authority = AuthorityBroker(store=self.store, clock_ms=lambda: 1_000_000)
        self.lease = await authority.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="worker-1",
                artifact_digest=self.artifact_digest,
                permissions=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
                runtime_profile="gvisor",
                requested_lease_ms=10_000,
            ),
            policy=TaskAuthorityPolicy(
                allowed=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
                max_lease_ms=30_000,
            ),
        )
        self.service = EvidenceService(store=self.store, clock_ms=lambda: 1_000_001)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_evidence_redacts_secrets_and_binds_claim_to_lease_and_artifact(self):
        row = await self.service.record(
            task_id="task-1",
            kind="tool.result",
            producer_identity="runtime:gvisor",
            claims={
                "result": "Authorization: Bearer abcdefghijklmnop",
                "token": "do-not-store",
                "verified": True,
            },
            artifact_digest=self.artifact_digest,
            lease_id=self.lease.lease_id,
        )
        self.assertEqual(row.task_id, "task-1")
        self.assertEqual(row.lease_id, self.lease.lease_id)
        self.assertNotIn("abcdefghijklmnop", str(row.claims))
        self.assertNotIn("do-not-store", str(row.claims))
        self.assertTrue(row.digest.startswith("sha256:"))

    async def test_task_evidence_forms_verifiable_append_only_chain(self):
        first = await self.service.record(
            task_id="task-1",
            kind="tool.step",
            producer_identity="runtime:gvisor",
            claims={"step": 1},
            artifact_digest=self.artifact_digest,
            lease_id=self.lease.lease_id,
        )
        second = await self.service.record(
            task_id="task-1",
            kind="tool.step",
            producer_identity="runtime:gvisor",
            claims={"step": 2},
            artifact_digest=self.artifact_digest,
            lease_id=self.lease.lease_id,
        )
        self.assertEqual(first.sequence, 1)
        self.assertIsNone(first.previous_digest)
        self.assertEqual(second.sequence, 2)
        self.assertEqual(second.previous_digest, first.digest)
        verified = await self.service.verify_chain("task-1")
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["count"], 2)
        self.assertEqual(verified["headDigest"], second.digest)

        async with self.store._session_factory() as db:
            await db.execute(
                update(CapabilityOsEvidence)
                .where(CapabilityOsEvidence.evidence_id == second.evidence_id)
                .values(claims={"step": "tampered"})
            )
            await db.commit()
        tampered = await self.service.verify_chain("task-1")
        self.assertFalse(tampered["valid"])
        self.assertEqual(tampered["error"], "digest-mismatch")
        self.assertEqual(tampered["errorSequence"], 2)

    async def test_evidence_rejects_cross_task_or_cross_artifact_lease_binding(self):
        with self.assertRaises(EvidenceViolation):
            await self.service.record(
                task_id="task-other",
                kind="tool.result",
                producer_identity="runtime:gvisor",
                claims={"verified": True},
                artifact_digest=self.artifact_digest,
                lease_id=self.lease.lease_id,
            )
        with self.assertRaises(EvidenceViolation):
            await self.service.record(
                task_id="task-1",
                kind="tool.result",
                producer_identity="runtime:gvisor",
                claims={"verified": True},
                artifact_digest="sha256:" + "0" * 64,
                lease_id=self.lease.lease_id,
            )


if __name__ == "__main__":
    unittest.main()
