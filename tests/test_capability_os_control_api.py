import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.routers import capability_os as capability_os_module
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
    digest_payload,
)
from cptr.services.capability_os.credential_broker import CredentialBroker
from cptr.services.capability_os.evidence import EvidenceService
from cptr.services.capability_os.evolution import EvolutionGate
from cptr.services.capability_os.evolution_engine import EvolutionExperimentEngine
from cptr.services.capability_os.forge import ContentAddressedBlobStore, ToolForge
from cptr.services.capability_os.mcp_fabric import McpFabric
from cptr.services.capability_os.mcp_remote import (
    McpAcquisitionService,
    RemoteMcpObservation,
    RemoteMcpTool,
)
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

    async def bootstrap(self, *, user_id):
        self.context = CapabilityTaskContext(
            task_id="wbs_bootstrap",
            user_id=user_id,
            workspace_id=None,
            source="workbench",
            status="OPEN",
            active=True,
            execution_allowed=True,
        )
        return self.context

    async def require_active(self, *, user_id, task_id):
        if user_id != self.context.user_id or task_id != self.context.task_id:
            raise KeyError("task")
        return self.context

    async def require_executable(self, *, user_id, task_id):
        return await self.require_active(user_id=user_id, task_id=task_id)

    async def fork_many(self, *, user_id, parent_task_id, count, cohort_id=None):
        await self.require_executable(user_id=user_id, task_id=parent_task_id)
        self.cohort_id = cohort_id
        return tuple(
            CapabilityTaskContext(
                task_id=f"child-{index + 1}",
                user_id=user_id,
                workspace_id=self.context.workspace_id,
                source="workbench",
                status="OPEN",
                active=True,
                execution_allowed=True,
                label=f"Capability OS Subagent {index + 1:02d}",
            )
            for index in range(count)
        )


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

    def test_forge_request_allows_taskless_bootstrap_without_loosening_acquire(self):
        forge_request = getattr(capability_os_module, "ForgeRequest", None)
        self.assertIsNotNone(forge_request)
        parsed = forge_request.model_validate({"operation": "bootstrap", "payload": {}})
        self.assertIsNone(parsed.task_id)
        with self.assertRaises(ValidationError):
            capability_os_module.OperationRequest.model_validate(
                {"operation": "discover", "payload": {}}
            )

    async def test_router_records_correlated_capability_lifecycle(self):
        request = SimpleNamespace(
            headers={
                "x-cptr-trace-id": "trace-capability-1",
                "x-cptr-request-id": "request-1",
                "x-cptr-mcp-session-id": "session-1",
                "x-cptr-tool-name": "cptr_factory",
            },
            app=SimpleNamespace(state=SimpleNamespace()),
        )
        body = capability_os_module.ResolveRequest(
            task_id="task-1",
            required=[{"action": "fs.read", "resource": "workspace"}],
        )
        service = SimpleNamespace(
            resolve=AsyncMock(return_value={"task": {"taskId": "task-1"}})
        )

        with (
            patch.object(capability_os_module, "_user", new=AsyncMock(return_value="user-1")),
            patch.object(capability_os_module, "_service", return_value=service),
            patch.object(
                capability_os_module.action_trace_store,
                "append",
                new=AsyncMock(return_value=True),
            ) as append_trace,
        ):
            result = await capability_os_module.resolve_capability_os(request, body)

        self.assertEqual(result["task"]["taskId"], "task-1")
        self.assertEqual(append_trace.await_count, 2)
        started = append_trace.await_args_list[0].kwargs
        completed = append_trace.await_args_list[1].kwargs
        self.assertEqual(started["trace_id"], "trace-capability-1")
        self.assertEqual(started["task_id"], "task-1")
        self.assertEqual(started["tool_name"], "cptr_factory")
        self.assertEqual(started["name"], "capability_os.resolve")
        self.assertEqual(started["status"], "started")
        self.assertEqual(completed["name"], "capability_os.resolve")
        self.assertEqual(completed["status"], "ok")
        self.assertGreaterEqual(completed["duration_ms"], 0)

    def test_all_core_operations_and_parallel_spawn_publish_capability_action_lifecycle(self):
        for handler, operation in (
            (capability_os_module.inspect_capability_os, "inspect"),
            (capability_os_module.resolve_capability_os, "resolve"),
            (capability_os_module.forge_capability_os, "forge"),
            (capability_os_module.execute_capability_os, "execute"),
            (capability_os_module.acquire_capability_os, "acquire"),
            (capability_os_module.reflect_capability_os, "reflect"),
            (capability_os_module.spawn_multiple_subagents_capability_os, "spawn_multiple_subagents"),
        ):
            source = inspect.getsource(handler)
            self.assertIn("_capability_action_trace(", source)
            self.assertIn(f'operation="{operation}"', source)

    def test_router_exposes_six_core_operations_plus_parallel_spawn(self):
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
                ("spawn-multiple-subagents", ("POST",)),
            },
        )

    async def test_parallel_spawn_prepares_isolated_child_contexts_without_agent_fallback(self):
        result = await self.service.spawn_multiple_subagents(
            user_id="user-1",
            task_id="task-1",
            objectives=("audit auth", "audit tests", "audit runtime"),
            cohort_id="cohort-77",
        )

        self.assertEqual(result["dispatch"]["cohortId"], "cohort-77")
        self.assertEqual(self.service.tasks.cohort_id, "cohort-77")
        self.assertEqual(result["dispatch"]["count"], 3)
        self.assertTrue(result["dispatch"]["parallel"])
        self.assertEqual(result["dispatch"]["startBarrier"], "all-child-contexts-ready")
        self.assertEqual(result["dispatch"]["fallback"], "none")
        children = result["dispatch"]["subagents"]
        self.assertEqual([item["task"]["taskId"] for item in children], ["child-1", "child-2", "child-3"])
        self.assertTrue(all(item["status"] == "READY_FOR_CLIENT_SAMPLING" for item in children))

    async def test_acquire_exposes_oauth_lifecycle_as_suboperations_without_seventh_primitive(self):
        class Started:
            def to_api(self):
                return {
                    "flowId": "flow-1",
                    "artifactDigest": "sha256:" + "a" * 64,
                    "authorizationUrl": "https://auth.example/authorize?state=redacted",
                    "status": "pending",
                    "expiresAtMs": 1_600_000,
                }

        class OAuth:
            def __init__(self):
                self.calls = []

            async def start(self, *, user_id, task_id, artifact_digest):
                self.calls.append(("start", user_id, task_id, artifact_digest))
                return Started()

            async def status(self, *, user_id, task_id, flow_id):
                self.calls.append(("status", user_id, task_id, flow_id))
                return {"flowId": flow_id, "status": "complete"}

            async def revoke(self, *, user_id, logical_name):
                self.calls.append(("revoke", user_id, logical_name))
                return True

        oauth = OAuth()
        self.service.mcp_oauth = oauth
        started = await self.service.acquire(
            user_id="user-1",
            task_id="task-1",
            operation="oauth-start",
            payload={"artifactDigest": "sha256:" + "1" * 64},
        )
        self.assertEqual(started["oauth"]["status"], "pending")
        status = await self.service.acquire(
            user_id="user-1",
            task_id="task-1",
            operation="oauth-status",
            payload={"flowId": "flow-1"},
        )
        self.assertEqual(status["oauth"]["status"], "complete")

        artifact = create_artifact(
            artifact_id="mcp.adapter.oauth",
            version="1",
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec={
                "serverId": "io.example/oauth",
                "remote": {"url": "https://mcp.example/mcp", "transport": "streamable-http"},
                "authentication": {
                    "mechanism": "bearer",
                    "logicalName": "mcp.oauth:user-bound",
                    "consumer": "mcp.remote:https://mcp.example/mcp",
                    "source": "oauth2",
                },
            },
            created_at="2026-09-08T10:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.EPHEMERAL,
        )
        await self.store.persist_artifact(artifact)
        revoked = await self.service.acquire(
            user_id="user-1",
            task_id="task-1",
            operation="oauth-revoke",
            payload={"artifactDigest": artifact.metadata.content_digest},
        )
        self.assertTrue(revoked["revoked"])
        self.assertEqual(
            oauth.calls,
            [
                ("start", "user-1", "task-1", "sha256:" + "1" * 64),
                ("status", "user-1", "task-1", "flow-1"),
                ("revoke", "user-1", "mcp.oauth:user-bound"),
            ],
        )
        with self.assertRaisesRegex(ValueError, "accepts only flowId"):
            await self.service.acquire(
                user_id="user-1",
                task_id="task-1",
                operation="oauth-status",
                payload={"flowId": "flow-1", "credential": "caller-value"},
            )

    async def test_forge_bootstrap_creates_task_without_existing_task_id(self):
        result = await self.service.forge(
            user_id="user-1",
            task_id="",
            operation="bootstrap",
            payload={},
        )

        self.assertTrue(result["bootstrapped"])
        self.assertEqual(result["task"]["taskId"], "wbs_bootstrap")
        self.assertEqual(result["task"]["source"], "workbench")
        self.assertEqual(result["task"]["status"], "OPEN")
        self.assertTrue(result["task"]["executionAllowed"])

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
        self.assertEqual(resolved["candidates"], [])
        self.assertEqual(resolved["acquisitionModes"], ["forge", "mcp"])

        updated = await self.store.set_artifact_state(digest, state=ArtifactState.QUALIFIED.value)
        self.assertTrue(updated)
        qualified = await self.service.resolve(
            user_id="user-1",
            task_id="task-1",
            required=(CapabilityRequest("filesystem.read", "repo:cptr/file.py"),),
            optional=(),
            forbidden=(),
        )
        self.assertEqual(qualified["candidates"][0]["contentDigest"], digest)

        reflected = await self.service.reflect(
            user_id="user-1",
            task_id="task-1",
            kind="capability.observation",
            claims={"outcome": "pass"},
        )
        self.assertTrue(reflected["evidenceId"].startswith("cevidence_"))
        rows = await self.store.list_evidence("task-1")
        self.assertEqual(rows[0].kind, "capability.observation")

    async def test_forge_exposes_full_tool_lifecycle_without_widening_authority(self):
        created = await self.service.forge(
            user_id="user-1",
            task_id="task-1",
            operation="create",
            payload={
                "toolId": "tool.lifecycle",
                "version": "1",
                "runtimeClass": "gvisor",
                "entrypoint": "main.py",
                "files": {"main.py": "print('v1')\n"},
                "requestedCapabilities": [
                    {"action": "filesystem.read", "resource": "repo:cptr/**"}
                ],
            },
        )
        digest = created["artifact"]["metadata"]["contentDigest"]
        inspected = await self.service.forge(
            user_id="user-1",
            task_id="task-1",
            operation="inspect",
            payload={"contentDigest": digest},
        )
        self.assertEqual(inspected["artifact"]["metadata"]["contentDigest"], digest)

        modified = await self.service.forge(
            user_id="user-1",
            task_id="task-1",
            operation="modify",
            payload={
                "contentDigest": digest,
                "version": "2",
                "files": {"main.py": "print('v2')\n"},
            },
        )
        self.assertEqual(modified["artifact"]["metadata"]["parent"], "tool.lifecycle@1#" + digest)
        self.assertNotEqual(modified["sourceDigest"], created["sourceDigest"])

        forked = await self.service.forge(
            user_id="user-1",
            task_id="task-1",
            operation="fork",
            payload={"contentDigest": digest, "toolId": "tool.lifecycle.fork", "version": "1"},
        )
        self.assertEqual(forked["artifact"]["metadata"]["id"], "tool.lifecycle.fork")
        self.assertEqual(forked["artifact"]["metadata"]["parent"], "tool.lifecycle@1#" + digest)
        self.assertEqual(await self.store.list_active_leases("task-1", now_ms=self.clock()), [])

    async def test_skill_genome_create_mutate_and_matched_evidence_promotion_are_server_owned(self):
        genome = {
            "objective": "Diagnose lifecycle inconsistencies",
            "assumptions": ["state is server authoritative"],
            "decompositionStrategy": ["inspect evidence", "compare lifecycle"],
            "decisionRules": ["prefer verified state"],
            "evidencePolicy": ["require machine evidence"],
            "toolSelectionHeuristics": ["prefer read-only inspection"],
            "stoppingConditions": ["invariant established"],
            "failureRecovery": ["collect more evidence"],
            "verificationRequirements": ["cross-check state"],
            "outputContract": "Return verified diagnosis",
            "activation": {"domains": ["cptr"], "taskPatterns": ["debug lifecycle"]},
            "context": {"resources": [], "maxInjectedTokens": 2000},
            "evaluation": {
                "benchmarkSuite": "capability-os-holdout",
                "primaryMetric": "verifier-pass-rate",
                "guardrailMetrics": ["policy-violations"],
            },
        }
        created = await self.service.forge(
            user_id="user-1", task_id="task-1", operation="skill-create",
            payload={"skillId": "skill.lifecycle", "version": "1", "genome": genome},
        )
        digest = created["artifact"]["metadata"]["contentDigest"]
        self.assertEqual(created["artifact"]["kind"], ArtifactKind.SKILL.value)

        mutated = await self.service.forge(
            user_id="user-1", task_id="task-1", operation="skill-mutate",
            payload={
                "contentDigest": digest,
                "version": "2",
                "operator": "simplify",
                "changes": {"decision_rules": ["prefer direct machine evidence"]},
            },
        )
        self.assertEqual(mutated["artifact"]["metadata"]["parent"], "skill.lifecycle@1#" + digest)

        common = {
            "modelId": "gpt-5.6-sol",
            "reasoningEffort": "high",
            "toolPermissionFingerprint": "perm-v1",
            "resourceBudgetFingerprint": "budget-v1",
            "taskDistributionFingerprint": "holdout-v1",
            "meanTokens": 1000,
            "meanToolCalls": 4,
        }
        result = await self.service.forge(
            user_id="user-1", task_id="task-1", operation="skill-promote",
            payload={
                "contentDigest": digest,
                "baseline": {**common, "runs": 10, "successes": 7, "regressions": 0, "policyViolations": 0},
                "candidate": {**common, "runs": 10, "successes": 9, "regressions": 0, "policyViolations": 0,
                              "meanTokens": 900, "meanToolCalls": 3},
            },
        )
        self.assertTrue(result["promoted"])
        self.assertEqual(result["artifactState"], ArtifactState.QUALIFIED.value)
        evidence = await self.store.list_evidence("task-1")
        self.assertTrue({"skill.evaluation", "skill.promotion"} <= {row.kind for row in evidence})

        with self.assertRaisesRegex(ValueError, "not matched on model_id"):
            await self.service.forge(
                user_id="user-1", task_id="task-1", operation="skill-evaluate",
                payload={
                    "contentDigest": mutated["artifact"]["metadata"]["contentDigest"],
                    "baseline": {**common, "runs": 10, "successes": 8, "regressions": 0, "policyViolations": 0},
                    "candidate": {**common, "modelId": "other-model", "runs": 10, "successes": 10,
                                  "regressions": 0, "policyViolations": 0},
                },
            )

    async def test_skill_portable_bundle_round_trip_is_server_evidenced_and_authority_free(self):
        genome = {
            "objective": "Inspect logs through semantic MCP hints",
            "assumptions": ["mount binding is server-owned"],
            "decompositionStrategy": ["inspect", "verify"],
            "decisionRules": ["prefer current projections"],
            "evidencePolicy": ["require server evidence"],
            "toolSelectionHeuristics": ["select by semantic tool name"],
            "stoppingConditions": ["verification complete"],
            "failureRecovery": ["reacquire projection"],
            "verificationRequirements": ["verify result"],
            "outputContract": "Return verified summary",
            "activation": {"domains": ["cptr"], "taskPatterns": ["inspect logs"]},
            "context": {"resources": [], "maxInjectedTokens": 1500},
            "evaluation": {
                "benchmarkSuite": "skill-portability",
                "primaryMetric": "pass-rate",
                "guardrailMetrics": ["policy-violations"],
            },
            "executionHints": {
                "mcpSteps": [
                    {"id": "inspect", "tool": "resource.logs", "timeoutMs": 5000}
                ]
            },
        }
        created = await self.service.forge(
            user_id="user-1",
            task_id="task-1",
            operation="skill-create",
            payload={"skillId": "skill.portable", "version": "1", "genome": genome},
        )
        source_digest = created["artifact"]["metadata"]["contentDigest"]
        exported = await self.service.forge(
            user_id="user-1",
            task_id="task-1",
            operation="skill-export",
            payload={"contentDigest": source_digest},
        )
        self.assertEqual(exported["bundle"]["apiVersion"], "cptr.io/skill-bundle/v1")
        self.assertTrue(exported["bundleDigest"].startswith("sha256:"))
        self.assertNotIn("permissions", str(exported["bundle"]).lower())
        self.assertNotIn("mountid", str(exported["bundle"]).lower())
        imported = await self.service.forge(
            user_id="user-1",
            task_id="task-1",
            operation="skill-import",
            payload={"bundle": exported["bundle"]},
        )
        self.assertEqual(imported["artifact"]["metadata"]["owner"], ArtifactOwner.USER.value)
        self.assertEqual(imported["artifact"]["metadata"]["origin"], ArtifactOrigin.IMPORTED.value)
        self.assertEqual(imported["artifact"]["metadata"]["sourceDigest"], exported["bundleDigest"])
        self.assertNotEqual(imported["artifact"]["metadata"]["contentDigest"], source_digest)
        evidence = await self.store.list_evidence("task-1")
        export_row = next(row for row in evidence if row.kind == "skill.export")
        import_row = next(row for row in evidence if row.kind == "skill.import")
        self.assertEqual(export_row.producer_identity, "capability-os-control")
        self.assertEqual(import_row.producer_identity, "capability-os-control")
        self.assertEqual(export_row.claims["bundleDigest"], exported["bundleDigest"])
        self.assertEqual(import_row.claims["bundleDigest"], exported["bundleDigest"])

        tampered = {
            **exported["bundle"],
            "spec": {**exported["bundle"]["spec"], "permissions": [{"action": "*"}]},
        }
        with self.assertRaisesRegex(ValueError, "authority field: permissions"):
            await self.service.forge(
                user_id="user-1",
                task_id="task-1",
                operation="skill-import",
                payload={"bundle": tampered},
            )

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
        supply_chain = built["build"]["supply_chain"]
        self.assertEqual(supply_chain["sbom"]["specVersion"], "SPDX-2.3")
        self.assertEqual(
            supply_chain["provenance"]["predicateType"],
            "https://slsa.dev/provenance/v1",
        )
        sbom = await self.service.forge(
            user_id="user-1", task_id="task-1", operation="supply-chain",
            payload={
                "contentDigest": digest,
                "documentDigest": supply_chain["sbom"]["digest"],
            },
        )
        self.assertEqual(sbom["document"]["spdxVersion"], "SPDX-2.3")
        provenance = await self.service.forge(
            user_id="user-1", task_id="task-1", operation="supply-chain",
            payload={
                "contentDigest": digest,
                "documentDigest": supply_chain["provenance"]["digest"],
            },
        )
        self.assertEqual(
            provenance["document"]["predicateType"],
            "https://slsa.dev/provenance/v1",
        )
        with self.assertRaisesRegex(KeyError, "supply-chain document not found"):
            await self.service.forge(
                user_id="user-1", task_id="task-1", operation="supply-chain",
                payload={"contentDigest": digest, "documentDigest": "sha256:" + "f" * 64},
            )
        evidence = await self.store.list_evidence("task-1")
        self.assertEqual({item.kind for item in evidence}, {"tool.build"})
        build_evidence = next(item for item in evidence if item.kind == "tool.build")
        self.assertEqual(build_evidence.claims["supplyChain"], supply_chain)
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

    async def test_reflect_experiment_lifecycle_derives_outcomes_from_server_evidence_before_explicit_promotion(self):
        control = create_artifact(
            artifact_id="tool.evolution.control",
            version="1",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec={"variant": "control"},
            created_at="2026-09-08T05:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        candidate = create_artifact(
            artifact_id="tool.evolution.candidate",
            version="2",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec={"variant": "candidate"},
            created_at="2026-09-08T05:00:01Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(control)
        await self.store.persist_artifact(candidate)
        gate = EvolutionGate(min_runs_per_arm=2, max_regression_rate=0.1)
        self.service.evolution = gate
        self.service.evolution_engine = EvolutionExperimentEngine(
            store=self.store,
            evidence=self.evidence,
            gate=gate,
            max_runs_per_arm=20,
        )
        created = await self.service.reflect(
            user_id="user-1",
            task_id="task-1",
            kind="ignored-when-experiment-present",
            claims={"caller": "cannot replace experiment lifecycle"},
            artifact_digest=candidate.metadata.content_digest,
            experiment={
                "operation": "create",
                "changeClass": "internal",
                "mode": "shadow",
                "hypothesis": "candidate improves verified success rate",
                "controlArtifactDigest": control.metadata.content_digest,
                "context": {
                    "modelId": "gpt-5.6-sol",
                    "reasoningEffort": "high",
                    "toolPermissionFingerprint": "perm-v1",
                    "resourceBudgetFingerprint": "budget-v1",
                    "taskDistributionFingerprint": "holdout-v1",
                },
                "minRunsPerArm": 2,
            },
        )
        experiment_id = created["experiment"]["experimentId"]
        self.assertTrue(experiment_id.startswith("evoexp_"))

        async def source(digest, *, success, cost):
            return await self.evidence.record(
                task_id="task-1",
                kind="capability.node",
                producer_identity="capability-vm",
                claims={
                    "status": "pass",
                    "verificationPassed": True,
                    "evolutionOutcome": {
                        "success": success,
                        "regression": False,
                        "safetyEvents": 0,
                        "cost": cost,
                    },
                },
                artifact_digest=digest,
            )

        control_sources = (
            await source(control.metadata.content_digest, success=True, cost=10.0),
            await source(control.metadata.content_digest, success=False, cost=12.0),
        )
        candidate_sources = (
            await source(candidate.metadata.content_digest, success=True, cost=8.0),
            await source(candidate.metadata.content_digest, success=True, cost=8.0),
        )
        for arm, rows in (("control", control_sources), ("candidate", candidate_sources)):
            for row in rows:
                observed = await self.service.reflect(
                    user_id="user-1",
                    task_id="task-1",
                    kind="ignored",
                    claims={},
                    experiment={
                        "operation": "observe",
                        "experimentId": experiment_id,
                        "arm": arm,
                        "sourceEvidenceId": row.evidence_id,
                    },
                )
                self.assertTrue(observed["observationEvidenceId"].startswith("cevidence_"))

        with self.assertRaisesRegex(ValueError, "unknown evolution experiment field: success"):
            await self.service.reflect(
                user_id="user-1",
                task_id="task-1",
                kind="ignored",
                claims={},
                experiment={
                    "operation": "observe",
                    "experimentId": experiment_id,
                    "arm": "candidate",
                    "sourceEvidenceId": candidate_sources[0].evidence_id,
                    "success": False,
                },
            )

        evaluated = await self.service.reflect(
            user_id="user-1",
            task_id="task-1",
            kind="ignored",
            claims={},
            experiment={"operation": "evaluate", "experimentId": experiment_id},
        )
        self.assertEqual(evaluated["evaluation"]["decision"], "promote")
        self.assertFalse(evaluated["promoted"])
        before = await self.store.get_artifact(candidate.metadata.content_digest)
        self.assertEqual(before.state, ArtifactState.QUALIFIED.value)

        promoted = await self.service.reflect(
            user_id="user-1",
            task_id="task-1",
            kind="ignored",
            claims={},
            promotion_target_state=ArtifactState.LEARNED.value,
            experiment={"operation": "promote", "experimentId": experiment_id},
        )
        self.assertTrue(promoted["promoted"])
        self.assertEqual(promoted["artifactState"], ArtifactState.LEARNED.value)
        after = await self.store.get_artifact(candidate.metadata.content_digest)
        self.assertEqual(after.state, ArtifactState.LEARNED.value)
        status = await self.service.reflect(
            user_id="user-1",
            task_id="task-1",
            kind="ignored",
            claims={},
            experiment={"operation": "status", "experimentId": experiment_id},
        )
        self.assertEqual(status["experiment"]["state"], "promoted")
        evidence = await self.store.list_evidence_for_run("task-1", experiment_id)
        kinds = [row.kind for row in evidence]
        self.assertEqual(kinds.count("evolution.experiment.observation"), 4)
        self.assertIn("evolution.experiment.evaluated", kinds)
        self.assertIn("evolution.experiment.promotion", kinds)
        self.assertNotIn("ignored-when-experiment-present", kinds)

    async def test_reflect_legacy_aggregate_comparison_is_informational_and_cannot_promote(self):
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
        self.assertFalse(result["promoted"])
        self.assertEqual(result["promotionBlocked"], "durable-experiment-required")
        row = await self.store.get_artifact(artifact.metadata.content_digest)
        self.assertEqual(row.state, ArtifactState.QUALIFIED.value)
        evidence = await self.store.list_evidence("task-1")
        kinds = {item.kind for item in evidence}
        self.assertTrue({"experiment.summary", "evolution.evaluation"} <= kinds)
        self.assertNotIn("evolution.promotion", kinds)

    async def test_reflect_legacy_authority_critical_summary_cannot_bypass_durable_experiment(self):
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
        self.assertEqual(denied["promotionBlocked"], "durable-experiment-required")

        approvals = []
        async def verify(approval_id, context):
            approvals.append((approval_id, context))
            return approval_id == "approval-verified"
        self.service.evolution_approval_verifier = verify
        still_blocked = await self.service.reflect(
            user_id="user-1", task_id="task-1", kind="experiment.authority",
            claims={}, artifact_digest=artifact.metadata.content_digest,
            comparison=comparison, change_class="authority-critical",
            promotion_target_state=ArtifactState.LEARNED.value,
            owner_approval_id="approval-verified",
        )
        self.assertFalse(still_blocked["promoted"])
        self.assertEqual(still_blocked["promotionBlocked"], "durable-experiment-required")
        self.assertEqual(approvals, [])
        row = await self.store.get_artifact(artifact.metadata.content_digest)
        self.assertEqual(row.state, ArtifactState.QUALIFIED.value)

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

    async def test_disabled_capability_os_approval_guard_keeps_standing_policy_authoritative(self):
        critical = CapabilityRequest("production.deploy", "service:cptr-backend")
        artifact = create_artifact(
            artifact_id="tool.production.deploy",
            version="1",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec={"runtime": {"class": "gvisor"}},
            created_at="2026-09-10T00:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(artifact)
        self.service.policy_provider = StandingAuthorityPolicyProvider((
            StandingAuthorityRule(
                policy=TaskAuthorityPolicy(allowed=(critical,), max_lease_ms=5000),
                user_id="user-1",
                workspace_id="ws-1",
            ),
        ))

        with patch(
            "cptr.services.capability_os.control.guard_policy_service.is_enabled",
            new=AsyncMock(return_value=False),
        ) as guard_enabled:
            lease, automatic = await self.service._lease(
                task=self.service.tasks.context,
                artifact_digest=artifact.metadata.content_digest,
                workload_id="workload-critical",
                permissions=(critical,),
                runtime_profile="gvisor",
            )

        self.assertTrue(automatic)
        self.assertIsNone(lease.approval_id)
        guard_enabled.assert_awaited_once_with(
            "user-1", "capability_os_external_approval"
        )
        await self.authority.revoke(lease.lease_id)

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

    async def test_authenticated_remote_mcp_mount_uses_server_credential_authority_and_evidence(self):
        remote_url = "https://mcp.example.test/mcp"
        logical_name = "mcp.oauth.logs"
        server_id = "io.example/logs"
        permission = CapabilityRequest("mcp.invoke", f"mcp:{server_id}/*")
        observation = RemoteMcpObservation(
            remote_url=remote_url,
            protocol_version="2025-06-18",
            server_name="example-logs",
            server_version="2.1.0",
            tools=(
                RemoteMcpTool("resource.logs", digest_payload({"type": "object"})),
            ),
        )

        class Connector:
            def __init__(self):
                self.probes = []
                self.invocations = []

            async def probe(
                self,
                remote_url,
                *,
                credential_broker=None,
                credential_lease=None,
                credential_name=None,
            ):
                self.probes.append(
                    (remote_url, credential_broker, credential_lease, credential_name)
                )
                return observation

            async def invoke(
                self,
                *,
                remote_url,
                tool_name,
                arguments,
                credential_broker=None,
                credential_lease=None,
                credential_name=None,
            ):
                self.invocations.append(
                    (remote_url, tool_name, arguments, credential_broker, credential_lease, credential_name)
                )
                return ActionResult(
                    output={"entries": ["ok"], "arguments": arguments},
                    verification_passed=True,
                    metadata={"transport": "streamable-http"},
                )

            async def release(self, **_kwargs):
                return None

        connector = Connector()
        credential_broker = CredentialBroker(clock_ms=self.clock)
        quarantined = create_artifact(
            artifact_id="mcp.adapter.auth-logs",
            version="2.1.0",
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec={
                "serverId": server_id,
                "acquisitionTaskId": "task-1",
                "remote": {"url": remote_url, "transport": "streamable-http"},
                "authentication": {
                    "mechanism": "bearer",
                    "logicalName": logical_name,
                    "consumer": f"mcp.remote:{remote_url}",
                },
                "permissions": [permission.to_dict()],
                "qualification": {
                    "state": "credential-required",
                    "identity": "registry+endpoint-validation",
                    "auth": "server-owned-logical-credential",
                },
            },
            created_at="2026-09-08T04:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            source_digest="sha256:" + "d" * 64,
            state=ArtifactState.EPHEMERAL,
        )
        await self.store.persist_artifact(quarantined)
        acquisition = McpAcquisitionService(
            store=self.store,
            fabric=self.service.fabric,
            discovery=FactoryDiscovery(providers=()),
            connector=connector,
            credential_broker=credential_broker,
            clock_ms=self.clock,
        )
        self.service.mcp_acquisition = acquisition
        self.service.mcp_connector = connector
        self.service.credential_broker = credential_broker
        self.service.policy_provider = StandingAuthorityPolicyProvider((
            StandingAuthorityRule(
                policy=TaskAuthorityPolicy(
                    allowed=(permission,),
                    max_lease_ms=10_000,
                    outbound_network="allow-list",
                    network_destinations=(remote_url,),
                    credential_names=(logical_name,),
                ),
                user_id="user-1",
                workspace_id="ws-1",
                workload_pattern="mcp*",
            ),
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

        self.assertEqual(result["mount"]["transportKind"], "streamable-http")
        self.assertEqual(result["mount"]["projectedTools"], ["resource.logs"])
        self.assertTrue(result["qualificationEvidenceId"].startswith("cevidence_"))
        self.assertEqual(len(connector.probes), 2)
        self.assertIs(connector.probes[0][1], credential_broker)
        self.assertEqual(connector.probes[0][3], logical_name)
        self.assertEqual(connector.probes[1][3], logical_name)
        self.assertNotEqual(connector.probes[0][2].lease_id, connector.probes[1][2].lease_id)

        active = await self.store.list_active_leases("task-1", now_ms=self.clock())
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].lease_id, result["mount"]["leaseId"])
        self.assertEqual(active[0].credentials["logicalNames"], [logical_name])
        self.assertEqual(active[0].network["destinations"], [remote_url])

        invoked = await self.service.acquire(
            user_id="user-1",
            task_id="task-1",
            operation="invoke",
            payload={
                "mountId": result["mount"]["mountId"],
                "tool": "resource.logs",
                "inputs": {"limit": 5},
                "timeoutMs": 2000,
            },
        )
        self.assertEqual(invoked["result"]["output"], {"entries": ["ok"], "arguments": {"limit": 5}})
        self.assertTrue(invoked["automaticLease"])
        self.assertEqual(len(connector.invocations), 1)
        self.assertEqual(connector.invocations[0][0:3], (remote_url, "resource.logs", {"limit": 5}))
        self.assertIs(connector.invocations[0][3], credential_broker)
        self.assertEqual(connector.invocations[0][5], logical_name)
        active_after_invoke = await self.store.list_active_leases("task-1", now_ms=self.clock())
        self.assertEqual([lease.lease_id for lease in active_after_invoke], [result["mount"]["leaseId"]])

        qualified = await self.store.get_artifact(result["mount"]["digest"])
        self.assertEqual(qualified.state, ArtifactState.QUALIFIED.value)
        self.assertEqual(qualified.spec["authentication"]["logicalName"], logical_name)
        self.assertNotIn("token", qualified.spec["authentication"])
        self.assertNotIn("headers", qualified.spec)
        evidence = await self.store.list_evidence("task-1")
        auth_evidence = next(row for row in evidence if row.kind == "mcp.remote.qualification")
        self.assertEqual(auth_evidence.claims["authentication"], "credential-brokered-bearer")
        self.assertNotIn("token", auth_evidence.claims)

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
