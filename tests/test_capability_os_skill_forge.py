import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.capability_os.authority import AuthorityBroker, LeaseRequest, TaskAuthorityPolicy
from cptr.services.capability_os.compiler import CapabilityCompiler
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    CapabilityRequest,
    create_artifact,
)
from cptr.services.capability_os.skill_forge import (
    SkillEvaluationArm,
    SkillEvaluationError,
    SkillEvaluator,
    SkillForge,
    SkillGenome,
    SkillMcpActivator,
    SkillMcpStep,
)
from cptr.services.capability_os.store import SqlCapabilityOsStore


class CapabilityOsSkillForgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=sessions)
        self.clock = lambda: 1_000_000
        self.forge = SkillForge(store=self.store, clock_ms=self.clock)
        self.authority = AuthorityBroker(store=self.store, clock_ms=self.clock)
        self.genome = SkillGenome(
            objective="debug distributed lifecycle defects",
            assumptions=("server evidence is authoritative",),
            decomposition_strategy=("reproduce", "trace causality", "falsify root cause"),
            decision_rules=("prefer machine evidence",),
            evidence_policy=("require a reproducer before patching",),
            tool_selection_heuristics=("reuse verified capability before forging",),
            stopping_conditions=("acceptance verifier passes",),
            failure_recovery=("return to root-cause analysis",),
            verification_requirements=("targeted", "regression", "live"),
            output_contract="report evidence and residual risk",
            activation_domains=("software-engineering",),
            activation_task_patterns=("distributed lifecycle",),
            activation_exclusions=(),
            context_resources=("factory evidence",),
            max_injected_tokens=4000,
            benchmark_suite="factory-lifecycle-v1",
            primary_metric="verifier_pass_rate",
            guardrail_metrics=("policy_violations", "regression_rate"),
            mcp_steps=(
                SkillMcpStep(
                    id="inspect",
                    tool_name="resource.inspect",
                    input_bindings={"mode": "summary"},
                    timeout_ms=5000,
                ),
                SkillMcpStep(
                    id="verify",
                    tool_name="resource.logs",
                    depends_on=("inspect",),
                    timeout_ms=5000,
                    verifier=True,
                ),
            ),
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_skill_genome_has_no_authority_and_is_stored_as_immutable_artifact(self):
        artifact = await self.forge.create(
            skill_id="debug.distributed-lifecycle",
            version="1",
            task_id="task-1",
            genome=self.genome,
        )
        self.assertEqual(artifact.state, ArtifactState.EPHEMERAL)
        payload = artifact.to_dict()
        self.assertNotIn("permissions", str(payload).lower())
        self.assertNotIn("credential", str(payload).lower())
        self.assertNotIn("mountId", str(payload))
        self.assertNotIn("serverId", str(payload))
        self.assertEqual(payload["spec"]["executionHints"]["mcpSteps"][0]["tool"], "resource.inspect")
        row = await self.store.get_artifact(artifact.metadata.content_digest)
        self.assertEqual(row.kind, "Skill")

    async def test_fork_and_mutation_preserve_lineage_and_parent(self):
        parent = await self.forge.create(
            skill_id="debug.distributed-lifecycle",
            version="1",
            task_id="task-1",
            genome=self.genome,
        )
        child = await self.forge.mutate(
            parent.metadata.content_digest,
            version="2",
            operator="simplify",
            changes={"max_injected_tokens": 2500},
        )
        self.assertEqual(child.metadata.parent, parent.identity)
        self.assertEqual(child.spec["lineage"]["operator"], "simplify")
        self.assertEqual(child.spec["context"]["maxInjectedTokens"], 2500)
        self.assertNotEqual(child.metadata.content_digest, parent.metadata.content_digest)

    async def test_mcp_activation_binds_only_active_projection_and_derives_critical_capability(self):
        skill = await self.forge.create(
            skill_id="debug.distributed-lifecycle",
            version="1",
            task_id="task-1",
            user_id="user-1",
            genome=self.genome,
        )
        await self.store.set_artifact_state(
            skill.metadata.content_digest, state=ArtifactState.QUALIFIED.value
        )
        adapter = create_artifact(
            artifact_id="mcp.adapter.vendor",
            version="2",
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec={"serverId": "vendor.logs"},
            created_at="2026-09-07T00:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(adapter)
        wildcard = CapabilityRequest("mcp.invoke", "mcp:vendor.logs/*")
        lease = await self.authority.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="mcp:vendor.logs",
                artifact_digest=adapter.metadata.content_digest,
                permissions=(wildcard,),
                runtime_profile="remote-mcp",
                requested_lease_ms=30_000,
            ),
            policy=TaskAuthorityPolicy(allowed=(wildcard,), max_lease_ms=30_000),
        )
        mount = await self.store.create_mcp_mount(
            task_id="task-1",
            server_id="vendor.logs",
            version="2",
            digest=adapter.metadata.content_digest,
            lease_id=lease.lease_id,
            projected_tools=("resource.inspect", "resource.logs"),
            transport_kind="streamable-http",
            now_ms=self.clock(),
        )
        activation = await SkillMcpActivator(
            store=self.store, authority=self.authority, clock_ms=self.clock
        ).activate(
            skill_digest=skill.metadata.content_digest,
            user_id="user-1",
            task_id="task-1",
        )
        capability = activation.capability
        self.assertEqual(capability.kind, ArtifactKind.CAPABILITY)
        self.assertEqual(capability.state, ArtifactState.EPHEMERAL)
        self.assertEqual(capability.metadata.user_id, "user-1")
        self.assertEqual(activation.mount_ids, (mount.mount_id,))
        self.assertEqual(
            activation.projected_actions,
            (f"mcp://{mount.mount_id}/resource.inspect", f"mcp://{mount.mount_id}/resource.logs"),
        )
        self.assertEqual(capability.spec["riskClass"], "critical")
        self.assertEqual(capability.spec["rollbackMode"], "irreversible")
        self.assertEqual(
            capability.spec["permissions"],
            [
                {"action": "mcp.invoke", "resource": "mcp:vendor.logs/resource.inspect"},
                {"action": "mcp.invoke", "resource": "mcp:vendor.logs/resource.logs"},
            ],
        )
        from cptr.services.capability_os.control import _capability_spec
        compiled = CapabilityCompiler().compile(_capability_spec(capability.spec))
        self.assertTrue(compiled.requires_human_approval)
        self.assertEqual(compiled.topological_order, ("inspect", "verify"))

        await self.store.release_mcp_mount(mount.mount_id, now_ms=self.clock())
        with self.assertRaises(PermissionError):
            await SkillMcpActivator(
                store=self.store, authority=self.authority, clock_ms=self.clock
            ).activate(
                skill_digest=skill.metadata.content_digest,
                user_id="user-1",
                task_id="task-1",
            )

    async def test_mcp_activation_rejects_unqualified_skill_and_ambiguous_projection(self):
        skill = await self.forge.create(
            skill_id="debug.distributed-lifecycle",
            version="1",
            task_id="task-1",
            user_id="user-1",
            genome=self.genome,
        )
        activator = SkillMcpActivator(store=self.store, authority=self.authority, clock_ms=self.clock)
        with self.assertRaises(PermissionError):
            await activator.activate(
                skill_digest=skill.metadata.content_digest, user_id="user-1", task_id="task-1"
            )

        await self.store.set_artifact_state(
            skill.metadata.content_digest, state=ArtifactState.QUALIFIED.value
        )
        adapter = create_artifact(
            artifact_id="mcp.adapter.ambiguous",
            version="1",
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec={"serverId": "vendor.ambiguous"},
            created_at="2026-09-07T00:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(adapter)
        wildcard = CapabilityRequest("mcp.invoke", "mcp:vendor.ambiguous/*")
        lease = await self.authority.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="mcp:vendor.ambiguous",
                artifact_digest=adapter.metadata.content_digest,
                permissions=(wildcard,),
                runtime_profile="remote-mcp",
                requested_lease_ms=30_000,
            ),
            policy=TaskAuthorityPolicy(allowed=(wildcard,), max_lease_ms=30_000),
        )
        for _ in range(2):
            await self.store.create_mcp_mount(
                task_id="task-1",
                server_id="vendor.ambiguous",
                version="1",
                digest=adapter.metadata.content_digest,
                lease_id=lease.lease_id,
                projected_tools=("resource.inspect", "resource.logs"),
                transport_kind="streamable-http",
                now_ms=self.clock(),
            )
        with self.assertRaisesRegex(PermissionError, "one active projected mount"):
            await activator.activate(
                skill_digest=skill.metadata.content_digest, user_id="user-1", task_id="task-1"
            )

    async def test_evaluator_requires_matched_budget_and_repeated_runs(self):
        evaluator = SkillEvaluator(min_runs_per_arm=5, max_regression_rate=0.02)
        baseline = SkillEvaluationArm(
            runs=5,
            successes=4,
            regressions=0,
            policy_violations=0,
            model_id="gpt-5.6-sol",
            reasoning_effort="high",
            tool_permission_fingerprint="same",
            resource_budget_fingerprint="same",
            task_distribution_fingerprint="holdout-a",
            mean_tokens=1000,
            mean_tool_calls=10,
        )
        candidate = SkillEvaluationArm(
            runs=5,
            successes=5,
            regressions=0,
            policy_violations=0,
            model_id="gpt-5.6-sol",
            reasoning_effort="high",
            tool_permission_fingerprint="same",
            resource_budget_fingerprint="same",
            task_distribution_fingerprint="holdout-a",
            mean_tokens=900,
            mean_tool_calls=8,
        )
        result = evaluator.compare(baseline=baseline, candidate=candidate)
        self.assertTrue(result.promotable)
        self.assertGreater(result.success_delta, 0)

        mismatched = SkillEvaluationArm(
            **{**candidate.__dict__, "reasoning_effort": "medium"}
        )
        with self.assertRaises(SkillEvaluationError):
            evaluator.compare(baseline=baseline, candidate=mismatched)

        tiny = SkillEvaluationArm(**{**candidate.__dict__, "runs": 1, "successes": 1})
        result = evaluator.compare(baseline=tiny, candidate=tiny)
        self.assertFalse(result.promotable)
        self.assertEqual(result.reason, "insufficient-evidence")


if __name__ == "__main__":
    unittest.main()
