import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.capability_os.contracts import ArtifactState, CapabilityRequest
from cptr.services.capability_os.forge import ContentAddressedBlobStore, CreateToolRequest, ToolForge
from cptr.services.capability_os.runtime import RuntimeBroker, RuntimeInventory, RuntimeUnavailable
from cptr.services.capability_os.store import SqlCapabilityOsStore


class CapabilityOsForgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=sessions)
        self.temp = tempfile.TemporaryDirectory()
        self.blobs = ContentAddressedBlobStore(Path(self.temp.name))
        self.runtime = RuntimeBroker(
            inventory=RuntimeInventory(
                runsc=None,
                wasmtime=None,
                firecracker=None,
                bubblewrap="/usr/bin/bwrap",
            ),
            production=True,
        )
        self.forge = ToolForge(store=self.store, blobs=self.blobs, runtime=self.runtime)

    async def asyncTearDown(self):
        self.temp.cleanup()
        await self.engine.dispose()

    async def test_create_is_content_addressed_persistent_but_grants_no_authority(self):
        draft = await self.forge.create(
            CreateToolRequest(
                tool_id="sse-causality-probe",
                version="1",
                task_id="task-1",
                runtime_class="gvisor",
                entrypoint="main.py",
                files={"main.py": "print('probe')\n"},
                requested_capabilities=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            )
        )
        self.assertEqual(draft.artifact.state, ArtifactState.EPHEMERAL)
        self.assertTrue(draft.source_digest.startswith("sha256:"))
        self.assertTrue(Path(draft.source_uri).is_file())
        self.assertEqual(await self.store.list_active_leases("task-1", now_ms=1_000_000), [])

    async def test_modify_and_fork_preserve_lineage_without_mutating_parent(self):
        original = await self.forge.create(
            CreateToolRequest(
                tool_id="probe",
                version="1",
                task_id="task-1",
                runtime_class="gvisor",
                entrypoint="main.py",
                files={"main.py": "print('v1')\n"},
                requested_capabilities=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            )
        )
        modified = await self.forge.modify(
            original.artifact.metadata.content_digest,
            version="2",
            files={"main.py": "print('v2')\n"},
        )
        forked = await self.forge.fork(
            original.artifact.metadata.content_digest,
            tool_id="probe-specialized",
            version="1",
        )
        self.assertNotEqual(modified.artifact.metadata.content_digest, original.artifact.metadata.content_digest)
        self.assertEqual(modified.artifact.metadata.parent, original.artifact.identity)
        self.assertEqual(forked.artifact.metadata.parent, original.artifact.identity)
        self.assertEqual(forked.artifact.metadata.id, "probe-specialized")
        reloaded = await self.store.get_artifact(original.artifact.metadata.content_digest)
        self.assertEqual(reloaded.version, "1")
        self.assertEqual(reloaded.state, ArtifactState.EPHEMERAL.value)

    async def test_production_build_fails_closed_without_approved_runtime(self):
        draft = await self.forge.create(
            CreateToolRequest(
                tool_id="probe",
                version="1",
                task_id="task-1",
                runtime_class="gvisor",
                entrypoint="main.py",
                files={"main.py": "print('x')\n"},
                requested_capabilities=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            )
        )
        with self.assertRaises(RuntimeUnavailable):
            await self.forge.build(draft.artifact.metadata.content_digest)

    async def test_external_broker_builder_owns_runtime_availability_not_web_process(self):
        calls = []
        async def builder(*, runtime_class, artifact, lease):
            calls.append((runtime_class.value, artifact.content_digest, lease.lease_id))
            return {
                "artifact_digest": "sha256:" + "a" * 64,
                "attestation": {"runtime": runtime_class.value, "broker": "root-owned"},
            }
        forge = ToolForge(store=self.store, blobs=self.blobs, runtime=self.runtime, builder=builder)
        draft = await forge.create(
            CreateToolRequest(
                tool_id="broker-built", version="1", task_id="task-1", runtime_class="gvisor",
                entrypoint="main.py", files={"main.py": "print(1)\n"},
                requested_capabilities=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            )
        )
        lease = SimpleNamespace(
            lease_id="lease-build", task_id="task-1", artifact_digest=draft.artifact.metadata.content_digest,
            runtime_profile="gvisor",
            permissions=(CapabilityRequest("runtime.build", f"artifact:{draft.artifact.metadata.content_digest}"),),
        )
        result = await forge.build(draft.artifact.metadata.content_digest, lease=lease)
        self.assertEqual(result.runtime_class, "gvisor")
        self.assertEqual(result.artifact_digest, "sha256:" + "a" * 64)
        self.assertEqual(calls, [("gvisor", draft.artifact.metadata.content_digest, "lease-build")])

        with self.assertRaises(PermissionError):
            await forge.build(draft.artifact.metadata.content_digest)

    async def test_qualified_tool_run_requires_runtime_run_lease_and_preserves_broker_attestation(self):
        calls = []

        async def runner(*, runtime_class, artifact, lease, inputs, timeout_ms):
            calls.append((runtime_class.value, artifact.content_digest, lease.lease_id, inputs, timeout_ms))
            return SimpleNamespace(
                output={"answer": 42},
                metadata={
                    "executionArtifactDigest": "sha256:" + "b" * 64,
                    "attestation": {"runtimeClass": runtime_class.value, "stdoutDigest": "sha256:" + "c" * 64},
                },
            )

        forge = ToolForge(store=self.store, blobs=self.blobs, runtime=self.runtime, runner=runner)
        draft = await forge.create(
            CreateToolRequest(
                tool_id="broker-run", version="1", task_id="task-1", runtime_class="gvisor",
                entrypoint="main.py", files={"main.py": "print('{\"answer\":42}')\n"},
                requested_capabilities=(CapabilityRequest("compute.transform", "input:json"),),
                user_id="user-1",
                resources={"cpuMillis": 1000, "memoryMiB": 128, "diskMiB": 64,
                           "pids": 8, "wallTimeMs": 5000, "maxOutputBytes": 65536},
            )
        )
        digest = draft.artifact.metadata.content_digest
        lease = SimpleNamespace(
            lease_id="lease-run", task_id="task-1", artifact_digest=digest,
            runtime_profile="gvisor", execution_context={"userId": "user-1"},
            permissions=(CapabilityRequest("runtime.run", f"artifact:{digest}"),),
        )
        with self.assertRaises(PermissionError):
            await forge.run(digest, inputs={"x": 21}, lease=lease, timeout_ms=2000)
        await self.store.set_artifact_state(digest, state=ArtifactState.QUALIFIED.value)
        result = await forge.run(digest, inputs={"x": 21}, lease=lease, timeout_ms=2000)
        self.assertEqual(result.output, {"answer": 42})
        self.assertEqual(result.execution_artifact_digest, "sha256:" + "b" * 64)
        self.assertEqual(result.attestation["runtimeClass"], "gvisor")

        reused_lease = SimpleNamespace(
            lease_id="lease-run-task-2", task_id="task-2", artifact_digest=digest,
            runtime_profile="gvisor", execution_context={"userId": "user-1"},
            permissions=(CapabilityRequest("runtime.run", f"artifact:{digest}"),),
        )
        reused = await forge.run(digest, inputs={"x": 22}, lease=reused_lease, timeout_ms=1500)
        self.assertEqual(reused.output, {"answer": 42})
        self.assertEqual(calls, [
            ("gvisor", digest, "lease-run", {"x": 21}, 2000),
            ("gvisor", digest, "lease-run-task-2", {"x": 22}, 1500),
        ])
        wrong_user = SimpleNamespace(
            lease_id="lease-run-user-2", task_id="task-2", artifact_digest=digest,
            runtime_profile="gvisor", execution_context={"userId": "user-2"},
            permissions=(CapabilityRequest("runtime.run", f"artifact:{digest}"),),
        )
        with self.assertRaisesRegex(PermissionError, "another user"):
            await forge.run(digest, inputs={}, lease=wrong_user, timeout_ms=1000)
        self.assertEqual(len(calls), 2)

    async def test_learned_promotion_requires_server_evolution_promotion_evidence(self):
        draft = await self.forge.create(
            CreateToolRequest(
                tool_id="evolving-tool",
                version="1",
                task_id="task-1",
                runtime_class="gvisor",
                entrypoint="main.py",
                files={"main.py": "print('{}')\n"},
                requested_capabilities=(CapabilityRequest("compute.transform", "input:json"),),
            )
        )
        digest = draft.artifact.metadata.content_digest
        await self.store.set_artifact_state(digest, state=ArtifactState.QUALIFIED.value)
        build = await self.store.append_evidence(
            task_id="task-1",
            kind="tool.build",
            producer_identity="capability-os-control",
            claims={"buildArtifactDigest": "sha256:" + "a" * 64, "attestation": {"ok": True}},
            artifact_digest=digest,
            created_at_ms=1_000_000,
        )
        with self.assertRaisesRegex(ValueError, "evolution promotion evidence"):
            await self.forge.persist(
                digest,
                target_state=ArtifactState.LEARNED,
                evidence_ids=(build.evidence_id,),
            )
        evaluation = await self.store.append_evidence(
            task_id="task-1",
            kind="evolution.evaluation",
            producer_identity="capability-os-control",
            claims={"decision": "promote"},
            artifact_digest=digest,
            created_at_ms=1_000_001,
        )
        promotion = await self.store.append_evidence(
            task_id="task-1",
            kind="evolution.promotion",
            producer_identity="capability-os-control",
            claims={
                "evaluationEvidenceId": evaluation.evidence_id,
                "targetState": ArtifactState.LEARNED.value,
            },
            artifact_digest=digest,
            created_at_ms=1_000_002,
        )
        await self.forge.persist(
            digest,
            target_state=ArtifactState.LEARNED,
            evidence_ids=(promotion.evidence_id,),
        )
        row = await self.store.get_artifact(digest)
        self.assertEqual(row.state, ArtifactState.LEARNED.value)

    async def test_destroy_retires_ephemeral_artifact_but_preserves_audit_record(self):
        draft = await self.forge.create(
            CreateToolRequest(
                tool_id="probe",
                version="1",
                task_id="task-1",
                runtime_class="gvisor",
                entrypoint="main.py",
                files={"main.py": "print('x')\n"},
                requested_capabilities=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            )
        )
        await self.forge.destroy(draft.artifact.metadata.content_digest)
        row = await self.store.get_artifact(draft.artifact.metadata.content_digest)
        self.assertEqual(row.state, ArtifactState.RETIRED.value)
        self.assertIsNotNone(row.retired_at_ms)


if __name__ == "__main__":
    unittest.main()
