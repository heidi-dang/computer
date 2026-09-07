import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.capability_os.authority import (
    AuthorityBroker,
    AuthorityDenied,
    LeaseRequest,
    TaskAuthorityPolicy,
)
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    CapabilityRequest,
    create_artifact,
    digest_payload,
)
from cptr.services.capability_os.evidence import EvidenceService
from cptr.services.capability_os.forge import ToolRunResult
from cptr.services.capability_os.generated_executor import ProjectedGeneratedToolExecutor
from cptr.services.capability_os.policy import SafeIsolationAuthorityPolicyProvider
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.services.capability_os.vm import ActionResult


class _BaseExecutor:
    def __init__(self):
        self.calls = []

    async def invoke(self, *, action_ref, version, inputs, lease, timeout_ms):
        self.calls.append((action_ref, version, inputs, lease.lease_id, timeout_ms))
        return ActionResult(output={"base": action_ref})


class _Forge:
    def __init__(self):
        self.calls = []

    async def run(self, content_digest, *, inputs, lease, timeout_ms):
        self.calls.append((content_digest, dict(inputs), lease, timeout_ms))
        return ToolRunResult(
            run_id="toolrun-test",
            source_digest="sha256:" + "3" * 64,
            runtime_class="gvisor",
            execution_artifact_digest="sha256:" + "4" * 64,
            output={"answer": 42, "input": dict(inputs)},
            attestation={"runtimeClass": "gvisor", "stdoutDigest": "sha256:" + "5" * 64},
        )


class CapabilityOsGeneratedExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=sessions)
        self.clock = lambda: 1_000_000
        self.authority = AuthorityBroker(store=self.store, clock_ms=self.clock)
        self.evidence = EvidenceService(store=self.store, clock_ms=lambda: 1_000_001)
        self.base = _BaseExecutor()
        self.forge = _Forge()
        self.policy = SafeIsolationAuthorityPolicyProvider(
            max_cpu_millis=2000,
            max_memory_mib=256,
            max_disk_mib=128,
            max_pids=32,
            max_wall_time_ms=30000,
            max_output_bytes=1048576,
        )

        capability = create_artifact(
            artifact_id="capability.generated-tool",
            version="1",
            kind=ArtifactKind.CAPABILITY,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.LEARNED,
            spec={"capabilityId": "capability.generated-tool"},
            created_at="2026-09-07T10:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(capability)
        self.capability_digest = capability.metadata.content_digest

        tool = create_artifact(
            artifact_id="tool.generated.answer",
            version="7",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec={
                "runtime": {"class": "gvisor", "entrypoint": "main.py"},
                "resources": {
                    "cpuMillis": 1000,
                    "memoryMiB": 128,
                    "diskMiB": 64,
                    "pids": 8,
                    "wallTimeMs": 5000,
                    "maxOutputBytes": 65536,
                },
            },
            created_at="2026-09-07T09:00:00Z",
            user_id="user-1",
            task_origin="task-that-forged-it",
            source_digest="sha256:" + "3" * 64,
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(tool)
        self.tool_digest = tool.metadata.content_digest
        self.tool_ref = f"tool://{self.tool_digest}"
        permission = CapabilityRequest("tool.invoke", f"artifact:{self.tool_digest}")
        self.vm_lease = await self.authority.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="capability:generated-tool",
                artifact_digest=self.capability_digest,
                permissions=(permission,),
                runtime_profile="cptr-vm",
                requested_lease_ms=30000,
                execution_context={
                    "userId": "user-1",
                    "workspaceId": "ws-1",
                    "taskSource": "workbench",
                },
            ),
            policy=TaskAuthorityPolicy(allowed=(permission,), max_lease_ms=30000),
        )
        self.executor = ProjectedGeneratedToolExecutor(
            base_executor=self.base,
            store=self.store,
            authority=self.authority,
            forge=self.forge,
            evidence=self.evidence,
            policy_provider=self.policy,
            clock_ms=self.clock,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_generated_tool_uses_separate_one_shot_runtime_lease_and_digest_only_evidence(self):
        result = await self.executor.invoke(
            action_ref=self.tool_ref,
            version="7",
            inputs={"x": 21},
            lease=self.vm_lease,
            timeout_ms=2000,
        )
        self.assertEqual(result.output["answer"], 42)
        self.assertEqual(self.base.calls, [])
        self.assertEqual(len(self.forge.calls), 1)
        digest, inputs, runtime_lease, timeout_ms = self.forge.calls[0]
        self.assertEqual(digest, self.tool_digest)
        self.assertEqual(inputs, {"x": 21})
        self.assertEqual(timeout_ms, 2000)
        self.assertEqual(runtime_lease.task_id, "task-1")
        self.assertEqual(runtime_lease.artifact_digest, self.tool_digest)
        self.assertEqual(runtime_lease.runtime_profile, "gvisor")
        self.assertEqual(runtime_lease.parent_lease_id, self.vm_lease.lease_id)
        self.assertEqual(runtime_lease.network["outbound"], "deny")
        active = await self.store.list_active_leases("task-1", now_ms=self.clock())
        self.assertEqual([row.lease_id for row in active], [self.vm_lease.lease_id])
        self.assertEqual(result.metadata["evidenceOutputPolicy"], "digest-only")
        self.assertEqual(result.metadata["outputDigest"], digest_payload(result.output))
        evidence = await self.store.list_evidence("task-1")
        run_evidence = next(row for row in evidence if row.kind == "tool.run")
        self.assertEqual(run_evidence.run_id, "toolrun-test")
        self.assertNotIn("output", run_evidence.claims)
        self.assertEqual(run_evidence.claims["executionArtifactDigest"], "sha256:" + "4" * 64)

    async def test_non_tool_action_delegates_without_issuing_runtime_authority(self):
        result = await self.executor.invoke(
            action_ref="cptr.fs.list",
            version="1",
            inputs={"path": "."},
            lease=self.vm_lease,
            timeout_ms=1000,
        )
        self.assertEqual(result.output, {"base": "cptr.fs.list"})
        self.assertEqual(len(self.base.calls), 1)
        self.assertEqual(self.forge.calls, [])

    async def test_wrong_version_fails_before_runtime_lease(self):
        with self.assertRaisesRegex(PermissionError, "version"):
            await self.executor.invoke(
                action_ref=self.tool_ref,
                version="8",
                inputs={},
                lease=self.vm_lease,
                timeout_ms=1000,
            )
        self.assertEqual(self.forge.calls, [])
        active = await self.store.list_active_leases("task-1", now_ms=self.clock())
        self.assertEqual([row.lease_id for row in active], [self.vm_lease.lease_id])

    async def test_missing_tool_invoke_permission_fails_closed(self):
        read = CapabilityRequest("filesystem.read", "repo:cptr/**")
        lease = await self.authority.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="capability:no-tool",
                artifact_digest=self.capability_digest,
                permissions=(read,),
                runtime_profile="cptr-vm",
                requested_lease_ms=30000,
                execution_context={
                    "userId": "user-1",
                    "workspaceId": "ws-1",
                    "taskSource": "workbench",
                },
            ),
            policy=TaskAuthorityPolicy(allowed=(read,), max_lease_ms=30000),
        )
        with self.assertRaises(AuthorityDenied):
            await self.executor.invoke(
                action_ref=self.tool_ref,
                version="7",
                inputs={},
                lease=lease,
                timeout_ms=1000,
            )
        self.assertEqual(self.forge.calls, [])

    async def test_user_scoped_tool_is_not_visible_to_another_lease_user(self):
        permission = CapabilityRequest("tool.invoke", f"artifact:{self.tool_digest}")
        other = await self.authority.issue(
            LeaseRequest(
                task_id="task-2",
                workload_id="capability:other-user",
                artifact_digest=self.capability_digest,
                permissions=(permission,),
                runtime_profile="cptr-vm",
                requested_lease_ms=30000,
                execution_context={
                    "userId": "user-2",
                    "workspaceId": "ws-2",
                    "taskSource": "workbench",
                },
            ),
            policy=TaskAuthorityPolicy(allowed=(permission,), max_lease_ms=30000),
        )
        with self.assertRaises(KeyError):
            await self.executor.invoke(
                action_ref=self.tool_ref,
                version="7",
                inputs={},
                lease=other,
                timeout_ms=1000,
            )
        self.assertEqual(self.forge.calls, [])


if __name__ == "__main__":
    unittest.main()
