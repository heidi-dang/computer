import tempfile
import unittest
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.routers.capability_os import capability_os_router
from cptr.services.capability_os.authority import AuthorityBroker, AuthorityDenied, TaskAuthorityPolicy
from cptr.services.capability_os.compiler import CapabilityCompiler
from cptr.services.capability_os.control import CapabilityOsControlService, CapabilityOsUnavailable
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    CapabilityRequest,
    create_artifact,
)
from cptr.services.capability_os.evidence import EvidenceService
from cptr.services.capability_os.evolution import EvolutionGate
from cptr.services.capability_os.forge import ContentAddressedBlobStore, ToolForge
from cptr.services.capability_os.mcp_fabric import McpFabric
from cptr.services.capability_os.mcp_remote import McpAcquisitionService
from cptr.services.capability_os.policy import (
    CompositeAuthorityPolicyProvider,
    SafeIsolationAuthorityPolicyProvider,
    StandingAuthorityPolicyProvider,
    StandingAuthorityRule,
)
from cptr.services.capability_os.resolver import CapabilityResolver
from cptr.services.capability_os.runtime import RuntimeBroker, RuntimeInventory
from cptr.services.capability_os.skill_forge import SkillMcpActivation
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.services.capability_os.tasks import CapabilityTaskContext
from cptr.services.capability_os.vm import ActionResult
from cptr.services.factory_discovery import FactoryDiscovery


class _Tasks:
    def __init__(self):
        self.context = CapabilityTaskContext(
            task_id="task-1",
            user_id="user-1",
            workspace_id="ws-1",
            source="workbench",
            status="RUNNING",
            active=True,
            execution_allowed=True,
        )

    async def require_active(self, *, user_id, task_id):
        if user_id != self.context.user_id or task_id != self.context.task_id:
            raise KeyError("task")
        return self.context

    async def require_executable(self, *, user_id, task_id):
        return await self.require_active(user_id=user_id, task_id=task_id)


class _Executor:
    def __init__(self):
        self.calls = []

    async def invoke(self, *, action_ref, version, inputs, lease, timeout_ms):
        self.calls.append((action_ref, lease.lease_id))
        return ActionResult(output={"action": action_ref})


class CapabilityOsControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=sessions)
        self.clock = lambda: 1_000_000
        self.authority = AuthorityBroker(store=self.store, clock_ms=self.clock)
        self.evidence = EvidenceService(store=self.store, clock_ms=self.clock)
        self.tempdir = tempfile.TemporaryDirectory()
        runtime = RuntimeBroker(
            inventory=RuntimeInventory(
                runsc=None,
                wasmtime=None,
                firecracker=None,
                bubblewrap="/usr/bin/bwrap",
            ),
            production=True,
        )
        forge = ToolForge(
            store=self.store,
            blobs=ContentAddressedBlobStore(Path(self.tempdir.name)),
            runtime=runtime,
            clock_ms=self.clock,
        )
        self.service = CapabilityOsControlService(
            store=self.store,
            tasks=_Tasks(),
            authority=self.authority,
            resolver=CapabilityResolver(store=self.store),
            forge=forge,
            compiler=CapabilityCompiler(),
            evidence=self.evidence,
            evolution=EvolutionGate(),
            runtime=runtime,
            fabric=McpFabric(store=self.store, authority=self.authority, clock_ms=self.clock),
            action_executor=None,
            mcp_connector=None,
            clock_ms=self.clock,
        )

    async def asyncTearDown(self):
        self.tempdir.cleanup()
        await self.engine.dispose()

    def test_router_exposes_exact_compact_six_operation_surface(self):
        actual = {
            (route.path.rsplit("/", 1)[-1], tuple(sorted(route.methods or ())))
            for route in capability_os_router.routes
        }
        self.assertEqual(
            actual,
            {
                ("inspect", ("GET",)),
                ("resolve", ("POST",)),
                ("forge", ("POST",)),
                ("execute", ("POST",)),
                ("acquire", ("POST",)),
                ("reflect", ("POST",)),
            },
        )

    async def test_forge_inspect_resolve_and_reflect_form_a_safe_control_plane(self):
        forged = await self.service.forge(
            user_id="user-1",
            task_id="task-1",
            operation="create",
            payload={
                "toolId": "tool.repo.reader",
                "version": "1",
                "runtimeClass": "gvisor",
                "entrypoint": "main.py",
                "files": {"main.py": "print('safe')\n"},
                "requestedCapabilities": [
                    {"action": "filesystem.read", "resource": "repo:cptr/**"}
                ],
            },
        )
        digest = forged["artifact"]["metadata"]["contentDigest"]
        inspected = await self.service.inspect(
            user_id="user-1",
            task_id="task-1",
            artifact_digest=digest,
        )
        self.assertEqual(inspected["task"]["source"], "workbench")
        self.assertEqual(inspected["artifact"]["metadata"]["contentDigest"], digest)
        self.assertFalse(inspected["runtime"]["generated_native_ready"])

        resolved = await self.service.resolve(
            user_id="user-1",
            task_id="task-1",
            required=(CapabilityRequest("filesystem.read", "repo:cptr/file.py"),),
            optional=(),
            forbidden=(),
        )
        self.assertEqual(resolved["candidates"][0]["contentDigest"], digest)

        reflected = await self.service.reflect(
            user_id="user-1",
            task_id="task-1",
            kind="capability.observation",
            claims={"outcome": "pass"},
        )
        self.assertTrue(reflected["evidenceId"].startswith("cevidence_"))
        rows = await self.store.list_evidence("task-1")
        self.assertEqual(rows[0].kind, "capability.observation")

    async def test_forge_build_uses_server_isolation_lease_records_evidence_and_revokes(self):
        calls = []
        async def builder(*, runtime_class, artifact, lease):
            calls.append(("build", runtime_class.value, lease.lease_id, dict(lease.network)))
            return {
                "artifact_digest": "sha256:" + "b" * 64,
                "attestation": {"builder": "root-broker", "sourceDigest": artifact.source_digest},
            }
        async def runner(*, runtime_class, artifact, lease, inputs, timeout_ms):
            calls.append(("run", runtime_class.value, lease.lease_id, dict(lease.network), inputs, timeout_ms))
            return ActionResult(
                output={"answer": 42},
                verification_passed=True,
                metadata={
                    "executionArtifactDigest": "sha256:" + "c" * 64,
                    "attestation": {"runtimeClass": "gvisor", "stdoutDigest": "sha256:" + "d" * 64},
                },
            )
        self.service.forge_impl._builder = builder
        self.service.forge_impl._runner = runner
        self.service.policy_provider = SafeIsolationAuthorityPolicyProvider(
            max_cpu_millis=2000, max_memory_mib=256, max_disk_mib=128, max_pids=32,
            max_wall_time_ms=30000, max_output_bytes=1048576,
        )
        forged = await self.service.forge(
            user_id="user-1", task_id="task-1", operation="create",
            payload={
                "toolId": "tool.build.test", "version": "1", "runtimeClass": "gvisor",
                "entrypoint": "main.py", "files": {"main.py": "print(1)\n"},
                "resources": {"cpuMillis": 1000, "memoryMiB": 128, "diskMiB": 64,
                              "pids": 8, "wallTimeMs": 5000, "maxOutputBytes": 65536},
                "requestedCapabilities": [{"action": "filesystem.read", "resource": "repo:cptr/**"}],
            },
        )
        digest = forged["artifact"]["metadata"]["contentDigest"]
        built = await self.service.forge(
            user_id="user-1", task_id="task-1", operation="build",
            payload={"contentDigest": digest},
        )
        self.assertTrue(built["automaticLease"])
        self.assertTrue(built["evidenceId"].startswith("cevidence_"))
        self.assertEqual(built["build"]["artifact_digest"], "sha256:" + "b" * 64)
        self.assertEqual(calls[0][0:2], ("build", "gvisor"))
        self.assertEqual(calls[0][3]["outbound"], "deny")
        self.assertEqual(await self.store.list_active_leases("task-1", now_ms=self.clock()), [])
        evidence = await self.store.list_evidence("task-1")
        self.assertEqual({item.kind for item in evidence}, {"tool.build"})
        promoted = await self.service.forge(
            user_id="user-1", task_id="task-1", operation="persist",
            payload={"contentDigest": digest, "targetState": ArtifactState.QUALIFIED.value,
                     "evidenceIds": [built["evidenceId"]]},
        )
        self.assertEqual(promoted["artifact"]["state"], ArtifactState.QUALIFIED.value)
        ran = await self.service.forge(
            user_id="user-1", task_id="task-1", operation="run",
            payload={"contentDigest": digest, "inputs": {"x": 21}, "timeoutMs": 2000},
        )
        self.assertTrue(ran["automaticLease"])
        self.assertEqual(ran["run"]["output"], {"answer": 42})
        self.assertEqual(ran["run"]["execution_artifact_digest"], "sha256:" + "c" * 64)
        self.assertEqual(calls[1][0:2], ("run", "gvisor"))
        self.assertEqual(calls[1][3]["outbound"], "deny")
        self.assertEqual(calls[1][4], {"x": 21})
        self.assertEqual(calls[1][5], 2000)
        self.assertEqual(await self.store.list_active_leases("task-1", now_ms=self.clock()), [])
        evidence = await self.store.list_evidence("task-1")
        self.assertEqual({item.kind for item in evidence}, {"tool.build", "tool.run"})
        run_evidence = next(item for item in evidence if item.kind == "tool.run")
        self.assertEqual(run_evidence.run_id, ran["run"]["run_id"])
        self.assertNotIn("output", run_evidence.claims)

    async def test_skill_mcp_activation_is_server_bound_records_evidence_and_rejects_caller_mounts(self):
        capability = create_artifact(
            artifact_id="skill.mcp.debug",
            version="1",
            kind=ArtifactKind.CAPABILITY,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.LEARNED,
            spec={
                "capabilityId": "skill.mcp.debug",
                "version": "1",
                "nodes": [{
                    "id": "inspect",
                    "actionRef": "mcp://mount-server/resource.inspect",
                    "version": "2",
                    "permissions": [{"action": "mcp.invoke", "resource": "mcp:vendor.logs/resource.inspect"}],
                    "approval": "human",
                }],
                "edges": [],
                "verifiers": [],
                "effects": [{"action": "mcp.invoke", "resource": "mcp:vendor.logs/resource.inspect"}],
                "permissions": [{"action": "mcp.invoke", "resource": "mcp:vendor.logs/resource.inspect"}],
                "rollbackMode": "irreversible",
                "deadlineMs": 5000,
                "maxParallelism": 1,
                "riskClass": "critical",
            },
            created_at="2026-09-07T07:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            source_digest="sha256:" + "a" * 64,
        )
        await self.store.persist_artifact(capability)
        activation = SkillMcpActivation(
            capability=capability,
            skill_digest="sha256:" + "a" * 64,
            mount_ids=("mount-server",),
            projected_actions=("mcp://mount-server/resource.inspect",),
        )

        class Activator:
            def __init__(self):
                self.calls = []

            async def activate(self, *, skill_digest, user_id, task_id):
                self.calls.append((skill_digest, user_id, task_id))
                return activation

        activator = Activator()
        self.service.skill_activator = activator
        result = await self.service.forge(
            user_id="user-1",
            task_id="task-1",
            operation="activate-skill-mcp",
            payload={"skillDigest": activation.skill_digest},
        )
        self.assertEqual(result["artifact"]["metadata"]["contentDigest"], capability.metadata.content_digest)
        self.assertEqual(result["mountIds"], ["mount-server"])
        self.assertEqual(activator.calls, [(activation.skill_digest, "user-1", "task-1")])
        evidence = await self.store.list_evidence("task-1")
        activation_evidence = next(row for row in evidence if row.kind == "skill.mcp.activation")
        self.assertEqual(activation_evidence.producer_identity, "capability-os-control")
        self.assertEqual(activation_evidence.claims["riskClass"], "critical")
        self.assertEqual(activation_evidence.claims["rollbackMode"], "irreversible")

        with self.assertRaisesRegex(ValueError, "accepts only skillDigest"):
            await self.service.forge(
                user_id="user-1",
                task_id="task-1",
                operation="activate-skill-mcp",
                payload={
                    "skillDigest": activation.skill_digest,
                    "mountId": "caller-forged",
                    "permissions": [{"action": "*", "resource": "*"}],
                },
            )
        self.assertEqual(len(activator.calls), 1)

    async def test_reflect_persists_matched_evolution_decision_and_promotes_qualified_artifact(self):
        artifact = create_artifact(
            artifact_id="tool.evolution",
            version="1",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec={"runtime": {"class": "gvisor", "entrypoint": "main.py"}},
            created_at="2026-09-07T07:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(artifact)
        common = {
            "modelId": "gpt-5.6-sol",
            "reasoningEffort": "high",
            "toolPermissionFingerprint": "perm-v1",
            "resourceBudgetFingerprint": "budget-v1",
            "taskDistributionFingerprint": "holdout-v1",
        }
        result = await self.service.reflect(
            user_id="user-1",
            task_id="task-1",
            kind="experiment.summary",
            claims={"experiment": "tool-evolution-v1"},
            artifact_digest=artifact.metadata.content_digest,
            comparison={
                "control": {**common, "runs": 10, "successes": 8, "regressions": 0,
                            "safetyEvents": 0, "meanCost": 10.0},
                "candidate": {**common, "runs": 10, "successes": 10, "regressions": 0,
                              "safetyEvents": 0, "meanCost": 9.0},
            },
            change_class="internal",
            promotion_target_state=ArtifactState.LEARNED.value,
        )
        self.assertEqual(result["promotionDecision"], "promote")
        self.assertTrue(result["promoted"])
        self.assertEqual(result["artifactState"], ArtifactState.LEARNED.value)
        row = await self.store.get_artifact(artifact.metadata.content_digest)
        self.assertEqual(row.state, ArtifactState.LEARNED.value)
        evidence = await self.store.list_evidence("task-1")
        kinds = {item.kind for item in evidence}
        self.assertTrue({"experiment.summary", "evolution.evaluation", "evolution.promotion"} <= kinds)
        promotion = next(item for item in evidence if item.kind == "evolution.promotion")
        self.assertEqual(promotion.claims["targetState"], ArtifactState.LEARNED.value)

    async def test_reflect_authority_critical_promotion_requires_server_verified_owner_approval(self):
        artifact = create_artifact(
            artifact_id="capability.evolution",
            version="1",
            kind=ArtifactKind.CAPABILITY,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.LEARNED,
            spec={"capabilityId": "capability.evolution", "version": "1", "nodes": []},
            created_at="2026-09-07T07:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(artifact)
        common = {
            "modelId": "gpt-5.6-sol",
            "reasoningEffort": "high",
            "toolPermissionFingerprint": "perm-v1",
            "resourceBudgetFingerprint": "budget-v1",
            "taskDistributionFingerprint": "holdout-v1",
        }
        comparison = {
            "control": {**common, "runs": 10, "successes": 8, "regressions": 0,
                        "safetyEvents": 0, "meanCost": 10.0},
            "candidate": {**common, "runs": 10, "successes": 10, "regressions": 0,
                          "safetyEvents": 0, "meanCost": 9.0},
        }
        denied = await self.service.reflect(
            user_id="user-1", task_id="task-1", kind="experiment.authority",
            claims={}, artifact_digest=artifact.metadata.content_digest,
            comparison=comparison, change_class="authority-critical",
            promotion_target_state=ArtifactState.LEARNED.value,
            owner_approval_id="approval-1",
        )
        self.assertEqual(denied["promotionDecision"], "owner-approval-required")
        self.assertFalse(denied["promoted"])

        approvals = []
        async def verify(approval_id, context):
            approvals.append((approval_id, context))
            return approval_id == "approval-verified"
        self.service.evolution_approval_verifier = verify
        approved = await self.service.reflect(
            user_id="user-1", task_id="task-1", kind="experiment.authority",
            claims={}, artifact_digest=artifact.metadata.content_digest,
            comparison=comparison, change_class="authority-critical",
            promotion_target_state=ArtifactState.LEARNED.value,
            owner_approval_id="approval-verified",
        )
        self.assertTrue(approved["promoted"])
        self.assertTrue(approvals)
        self.assertEqual(approvals[0][1]["artifactDigest"], artifact.metadata.content_digest)

    async def test_server_policy_can_auto_issue_one_shot_lease_without_accepting_caller_policy(self):
        read = CapabilityRequest("filesystem.read", "repo:cptr/**")
        artifact = create_artifact(
            artifact_id="capability.repo.read",
            version="1",
            kind=ArtifactKind.CAPABILITY,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.LEARNED,
            spec={
                "capabilityId": "capability.repo.read",
                "version": "1",
                "effects": [read.to_dict()],
                "nodes": [{
                    "id": "read",
                    "actionRef": "core:repo.read",
                    "version": "1",
                    "permissions": [read.to_dict()],
                }],
                "edges": [],
                "verifiers": [],
                "rollbackMode": "full",
                "permissions": [read.to_dict()],
                "deadlineMs": 1000,
                "maxParallelism": 1,
                "riskClass": "read",
            },
            created_at="2026-09-07T07:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(artifact)
        executor = _Executor()
        self.service.action_executor = executor
        self.service.policy_provider = StandingAuthorityPolicyProvider((
            StandingAuthorityRule(
                policy=TaskAuthorityPolicy(allowed=(read,), max_lease_ms=5000),
                user_id="user-1",
                workspace_id="ws-1",
            ),
        ))
        result = await self.service.execute(
            user_id="user-1",
            task_id="task-1",
            capability_digest=artifact.metadata.content_digest,
            lease_id=None,
            spec={"nodes": [{"actionRef": "malicious:replacement"}]},
            inputs={},
        )
        self.assertTrue(result["automaticLease"])
        self.assertEqual(executor.calls[0][0], "core:repo.read")
        self.assertEqual(await self.store.list_active_leases("task-1", now_ms=self.clock()), [])

        write = CapabilityRequest("filesystem.write", "repo:cptr/**")
        denied = create_artifact(
            artifact_id="capability.repo.write",
            version="1",
            kind=ArtifactKind.CAPABILITY,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.LEARNED,
            spec={
                "capabilityId": "capability.repo.write",
                "version": "1",
                "effects": [write.to_dict()],
                "nodes": [{"id": "write", "actionRef": "core:repo.write", "version": "1",
                           "permissions": [write.to_dict()]}],
                "edges": [], "verifiers": [], "rollbackMode": "full",
                "permissions": [write.to_dict()], "deadlineMs": 1000,
                "maxParallelism": 1, "riskClass": "reversible-write",
            },
            created_at="2026-09-07T07:00:01Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(denied)
        with self.assertRaises(AuthorityDenied):
            await self.service.execute(
                user_id="user-1", task_id="task-1",
                capability_digest=denied.metadata.content_digest,
                lease_id=None, spec={"policy": {"allow": "everything"}}, inputs={},
            )
        self.assertEqual(len(executor.calls), 1)

    async def test_execute_runs_content_addressed_generated_tool_with_separate_runtime_lease(self):
        tool = create_artifact(
            artifact_id="tool.vm.answer",
            version="3",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec={
                "runtime": {"class": "gvisor", "entrypoint": "main.py"},
                "resources": {"cpuMillis": 1000, "memoryMiB": 128, "diskMiB": 64,
                              "pids": 8, "wallTimeMs": 5000, "maxOutputBytes": 65536},
            },
            created_at="2026-09-07T08:00:00Z",
            user_id="user-1",
            task_origin="task-that-forged-it",
            source_digest="sha256:" + "a" * 64,
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(tool)
        tool_permission = CapabilityRequest("tool.invoke", f"artifact:{tool.metadata.content_digest}")
        capability = create_artifact(
            artifact_id="capability.vm.generated-tool",
            version="1",
            kind=ArtifactKind.CAPABILITY,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.LEARNED,
            spec={
                "capabilityId": "capability.vm.generated-tool",
                "version": "1",
                "effects": [tool_permission.to_dict()],
                "nodes": [{
                    "id": "run",
                    "actionRef": f"tool://{tool.metadata.content_digest}",
                    "version": "3",
                    "permissions": [tool_permission.to_dict()],
                    "timeoutMs": 2000,
                }],
                "edges": [],
                "verifiers": [],
                "rollbackMode": "partial",
                "permissions": [tool_permission.to_dict()],
                "deadlineMs": 5000,
                "maxParallelism": 1,
                "riskClass": "read",
            },
            created_at="2026-09-07T08:01:00Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(capability)

        runtime_leases = []
        async def runner(*, runtime_class, artifact, lease, inputs, timeout_ms):
            runtime_leases.append(lease)
            return ActionResult(
                output={"answer": 42, "inputs": inputs},
                verification_passed=True,
                metadata={
                    "executionArtifactDigest": "sha256:" + "b" * 64,
                    "attestation": {"runtimeClass": runtime_class.value,
                                    "stdoutDigest": "sha256:" + "c" * 64},
                },
            )

        self.service.forge_impl._runner = runner
        self.service.action_executor = _Executor()
        self.service.policy_provider = CompositeAuthorityPolicyProvider((
            SafeIsolationAuthorityPolicyProvider(
                max_cpu_millis=2000, max_memory_mib=256, max_disk_mib=128, max_pids=32,
                max_wall_time_ms=30000, max_output_bytes=1048576,
            ),
            StandingAuthorityPolicyProvider((
                StandingAuthorityRule(
                    policy=TaskAuthorityPolicy(allowed=(tool_permission,), max_lease_ms=5000),
                    user_id="user-1",
                    workspace_id="ws-1",
                    workload_pattern="capability:*",
                ),
            )),
        ))
        result = await self.service.execute(
            user_id="user-1",
            task_id="task-1",
            capability_digest=capability.metadata.content_digest,
            lease_id=None,
            spec={"nodes": [{"actionRef": "caller-cannot-replace-stored-recipe"}]},
            inputs={"x": 21},
        )
        self.assertTrue(result["automaticLease"])
        self.assertEqual(result["result"]["status"], "complete")
        self.assertEqual(result["result"]["outputs"]["run"]["answer"], 42)
        self.assertEqual(len(runtime_leases), 1)
        self.assertEqual(runtime_leases[0].task_id, "task-1")
        self.assertEqual(runtime_leases[0].artifact_digest, tool.metadata.content_digest)
        self.assertEqual(runtime_leases[0].runtime_profile, "gvisor")
        self.assertIsNotNone(runtime_leases[0].parent_lease_id)
        self.assertEqual(await self.store.list_active_leases("task-1", now_ms=self.clock()), [])
        evidence = await self.store.list_evidence("task-1")
        kinds = {row.kind for row in evidence}
        self.assertTrue({"tool.run", "capability.node"} <= kinds)
        tool_run = next(row for row in evidence if row.kind == "tool.run")
        self.assertNotIn("output", tool_run.claims)
        node = next(row for row in evidence if row.kind == "capability.node")
        self.assertNotIn("output", node.claims)
        self.assertTrue(node.claims["outputDigest"].startswith("sha256:"))

    async def test_packaged_mcp_mount_requires_gvisor_qualification_evidence_and_revokes_temp_lease(self):
        resources = {
            "cpuMillis": 1000,
            "memoryMiB": 128,
            "diskMiB": 64,
            "pids": 8,
            "wallTimeMs": 5000,
            "maxOutputBytes": 65536,
        }
        permission = CapabilityRequest("mcp.invoke", "mcp:io.example/package-logs/*")
        bundle_digest = "sha256:" + "a" * 64
        quarantined = create_artifact(
            artifact_id="mcp.adapter.package-logs",
            version="1.0.0",
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec={
                "serverId": "io.example/package-logs",
                "acquisitionTaskId": "task-1",
                "package": {
                    "registryType": "mcpb",
                    "transport": "stdio-gvisor",
                    "sha256": "b" * 64,
                    "bundleDigest": bundle_digest,
                    "manifestDigest": "sha256:" + "c" * 64,
                    "manifestVersion": "0.4",
                    "packageName": "io.example/package-logs",
                    "serverEntrypoint": "server/main.py",
                    "executable": False,
                },
                "runtime": {
                    "class": "gvisor",
                    "language": "python",
                    "entrypoint": "__cptr_mcp_bridge.py",
                },
                "resources": resources,
                "permissions": [permission.to_dict()],
                "qualification": {"state": "quarantined"},
            },
            created_at="2026-09-08T00:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            source_digest=bundle_digest,
            state=ArtifactState.EPHEMERAL,
        )
        await self.store.persist_artifact(quarantined)

        runtime_leases = []

        async def package_runner(*, runtime_class, artifact, lease, inputs, timeout_ms):
            runtime_leases.append(lease)
            self.assertEqual(runtime_class.value, "gvisor")
            self.assertEqual(inputs["mode"], "probe")
            self.assertEqual(dict(lease.network)["outbound"], "deny")
            return ActionResult(
                output={
                    "ok": True,
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "package-logs", "version": "1.0.0"},
                    "tools": [{"name": "resource.logs", "inputSchema": {"type": "object"}}],
                },
                verification_passed=True,
                metadata={
                    "attestation": {
                        "runtimeClass": "gvisor",
                        "network": "deny",
                        "sourceDigest": bundle_digest,
                    }
                },
            )

        acquisition = McpAcquisitionService(
            store=self.store,
            fabric=self.service.fabric,
            discovery=FactoryDiscovery(providers=()),
            connector=object(),
            package_runner=package_runner,
            package_resources=resources,
            clock_ms=self.clock,
        )
        self.service.mcp_acquisition = acquisition
        self.service.mcp_connector = object()
        self.service.policy_provider = CompositeAuthorityPolicyProvider((
            SafeIsolationAuthorityPolicyProvider(
                max_cpu_millis=2000,
                max_memory_mib=256,
                max_disk_mib=128,
                max_pids=32,
                max_wall_time_ms=30000,
                max_output_bytes=1048576,
            ),
            StandingAuthorityPolicyProvider((
                StandingAuthorityRule(
                    policy=TaskAuthorityPolicy(allowed=(permission,), max_lease_ms=5000),
                    user_id="user-1",
                    workspace_id="ws-1",
                    workload_pattern="mcp:*",
                ),
            )),
        ))

        result = await self.service.acquire(
            user_id="user-1",
            task_id="task-1",
            operation="mount",
            payload={
                "goal": {
                    "goal": "read logs",
                    "required": ["resource.logs"],
                    "optional": [],
                    "forbidden": ["resource.delete"],
                    "dataClassification": "private",
                },
                "artifactDigest": quarantined.metadata.content_digest,
            },
        )

        self.assertEqual(result["mount"]["transportKind"], "stdio-gvisor")
        self.assertEqual(result["mount"]["projectedTools"], ["resource.logs"])
        self.assertTrue(result["qualificationEvidenceId"].startswith("cevidence_"))
        self.assertEqual(len(runtime_leases), 1)
        active = await self.store.list_active_leases("task-1", now_ms=self.clock())
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].runtime_profile, "remote-mcp")
        self.assertEqual(active[0].lease_id, result["mount"]["leaseId"])
        evidence = await self.store.list_evidence("task-1")
        qualification = next(row for row in evidence if row.kind == "mcp.package.qualification")
        self.assertEqual(qualification.artifact_digest, quarantined.metadata.content_digest)
        self.assertEqual(qualification.claims["qualifiedArtifactDigest"], result["mount"]["digest"])
        self.assertEqual(qualification.claims["transport"], "stdio-gvisor")
        qualified = await self.store.get_artifact(result["mount"]["digest"])
        self.assertEqual(qualified.state, ArtifactState.QUALIFIED.value)
        self.assertTrue(qualified.spec["package"]["executable"])

    async def test_side_effecting_operations_fail_closed_without_runtime_adapters(self):
        with self.assertRaises(CapabilityOsUnavailable):
            await self.service.execute(
                user_id="user-1",
                task_id="task-1",
                capability_digest="sha256:" + "0" * 64,
                lease_id="lease_missing",
                spec={},
                inputs={},
            )

        with self.assertRaises(CapabilityOsUnavailable):
            await self.service.acquire(
                user_id="user-1",
                task_id="task-1",
                operation="qualify",
                payload={
                    "goal": {
                        "goal": "inspect logs",
                        "required": ["resource.logs"],
                        "optional": [],
                        "forbidden": ["resource.delete"],
                        "dataClassification": "private",
                    },
                    "query": "logs MCP",
                    "candidate": {
                        "serverId": "caller-forged",
                        "tools": ["resource.logs"],
                        "identityOk": True,
                        "authOk": True,
                        "sandboxable": True,
                    },
                },
            )

        with self.assertRaises(CapabilityOsUnavailable):
            await self.service.acquire(
                user_id="user-1",
                task_id="task-1",
                operation="mount",
                payload={
                    "goal": {
                        "goal": "inspect logs",
                        "required": ["resource.logs"],
                        "optional": [],
                        "forbidden": [],
                        "dataClassification": "private",
                    },
                    "artifactDigest": "sha256:" + "1" * 64,
                    "leaseId": "lease_missing",
                },
            )


if __name__ == "__main__":
    unittest.main()
