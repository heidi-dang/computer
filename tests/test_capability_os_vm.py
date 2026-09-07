import unittest
from dataclasses import replace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.capability_os.authority import AuthorityBroker, LeaseRequest, TaskAuthorityPolicy
from cptr.services.capability_os.compiler import CapabilityCompiler, CapabilitySpec, DagEdge, DagNode
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    CapabilityRequest,
    create_artifact,
)
from cptr.services.capability_os.evidence import EvidenceService
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.services.capability_os.vm import ActionResult, CapabilityVm, CapabilityVmError


class _Executor:
    def __init__(self, fail_ref=None):
        self.calls = []
        self.fail_ref = fail_ref

    async def invoke(self, *, action_ref, version, inputs, lease, timeout_ms):
        self.calls.append((action_ref, version, inputs, lease.lease_id))
        if action_ref == self.fail_ref:
            raise RuntimeError("injected failure")
        return ActionResult(
            output={"action": action_ref},
            verification_passed=True if "verify" in action_ref else None,
        )


class CapabilityOsVmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=sessions)
        artifact = create_artifact(
            artifact_id="capability.deploy",
            version="1",
            kind=ArtifactKind.CAPABILITY,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.LEARNED,
            spec={"recipe": "typed"},
            created_at="2026-09-07T01:00:00Z",
            task_origin="task-1",
        )
        await self.store.persist_artifact(artifact)
        self.digest = artifact.metadata.content_digest
        self.authority = AuthorityBroker(store=self.store, clock_ms=lambda: 1_000_000)
        permissions = (
            CapabilityRequest("filesystem.write", "repo:cptr/**"),
            CapabilityRequest("filesystem.read", "repo:cptr/**"),
        )
        self.lease = await self.authority.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="capability-vm",
                artifact_digest=self.digest,
                permissions=permissions,
                runtime_profile="cptr-vm",
                requested_lease_ms=30_000,
            ),
            policy=TaskAuthorityPolicy(allowed=permissions, max_lease_ms=30_000),
        )
        self.evidence = EvidenceService(store=self.store, clock_ms=lambda: 1_000_001)

    async def asyncTearDown(self):
        await self.engine.dispose()

    def _compiled(self):
        return CapabilityCompiler().compile(
            CapabilitySpec(
                capability_id="workflow.test",
                version="1",
                inputs_schema={"type": "object"},
                preconditions=(),
                effects=(CapabilityRequest("filesystem.write", "repo:cptr/**"),),
                nodes=(
                    DagNode(
                        id="write",
                        action_ref="tool:write",
                        version="1",
                        permissions=(CapabilityRequest("filesystem.write", "repo:cptr/**"),),
                        compensation_action_ref="tool:undo-write",
                    ),
                    DagNode(
                        id="verify",
                        action_ref="tool:verify",
                        version="1",
                        permissions=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
                    ),
                ),
                edges=(DagEdge("write", "verify"),),
                verifiers=("verify",),
                rollback_mode="compensating",
                permissions=(
                    CapabilityRequest("filesystem.write", "repo:cptr/**"),
                    CapabilityRequest("filesystem.read", "repo:cptr/**"),
                ),
                deadline_ms=30_000,
                max_parallelism=1,
                risk_class="reversible-write",
            )
        )

    async def test_vm_executes_compiled_order_and_records_machine_evidence(self):
        executor = _Executor()
        vm = CapabilityVm(executor=executor, evidence=self.evidence, clock_ms=lambda: 1_000_001)
        result = await vm.execute(
            task_id="task-1",
            capability_digest=self.digest,
            compiled=self._compiled(),
            lease=self.lease,
            inputs={"target": "repo:cptr"},
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual([call[0] for call in executor.calls], ["tool:write", "tool:verify"])
        self.assertEqual(result.completed_nodes, ("write", "verify"))
        evidence = await self.store.list_evidence("task-1")
        self.assertGreaterEqual(len(evidence), 2)

    async def test_digest_only_action_evidence_omits_raw_output_but_keeps_vm_output(self):
        class DigestOnlyExecutor:
            async def invoke(self, *, action_ref, version, inputs, lease, timeout_ms):
                return ActionResult(
                    output={"sensitive": "returned-not-persisted", "action": action_ref},
                    verification_passed=True,
                    metadata={
                        "evidenceOutputPolicy": "digest-only",
                        "outputDigest": "sha256:" + "9" * 64,
                        "attestation": {"runtimeClass": "gvisor"},
                    },
                )

        vm = CapabilityVm(
            executor=DigestOnlyExecutor(),
            evidence=self.evidence,
            clock_ms=lambda: 1_000_001,
        )
        result = await vm.execute(
            task_id="task-1",
            capability_digest=self.digest,
            compiled=self._compiled(),
            lease=self.lease,
            inputs={},
        )
        self.assertEqual(result.outputs["write"]["sensitive"], "returned-not-persisted")
        evidence = await self.store.list_evidence("task-1")
        node_rows = [row for row in evidence if row.kind == "capability.node"]
        self.assertEqual(len(node_rows), 2)
        for row in node_rows:
            self.assertNotIn("output", row.claims)
            self.assertEqual(row.claims["outputDigest"], "sha256:" + "9" * 64)
            self.assertEqual(row.claims["metadata"]["evidenceOutputPolicy"], "digest-only")

    async def test_failure_uses_declared_compensation_in_reverse_completed_order(self):
        compiled = CapabilityCompiler().compile(
            CapabilitySpec(
                capability_id="workflow.rollback",
                version="1",
                inputs_schema={},
                preconditions=(),
                effects=(CapabilityRequest("filesystem.write", "repo:cptr/**"),),
                nodes=(
                    DagNode(
                        id="first",
                        action_ref="tool:first",
                        version="1",
                        permissions=(CapabilityRequest("filesystem.write", "repo:cptr/**"),),
                        compensation_action_ref="tool:undo-first",
                    ),
                    DagNode(
                        id="second",
                        action_ref="tool:second",
                        version="1",
                        permissions=(CapabilityRequest("filesystem.write", "repo:cptr/**"),),
                        compensation_action_ref="tool:undo-second",
                    ),
                ),
                edges=(DagEdge("first", "second"),),
                verifiers=(),
                rollback_mode="compensating",
                permissions=(CapabilityRequest("filesystem.write", "repo:cptr/**"),),
                deadline_ms=30_000,
                max_parallelism=1,
                risk_class="reversible-write",
            )
        )
        executor = _Executor(fail_ref="tool:second")
        vm = CapabilityVm(executor=executor, evidence=self.evidence, clock_ms=lambda: 1_000_001)
        result = await vm.execute(
            task_id="task-1",
            capability_digest=self.digest,
            compiled=compiled,
            lease=self.lease,
            inputs={},
        )
        self.assertEqual(result.status, "rolled_back")
        self.assertEqual(result.completed_nodes, ("first",))
        self.assertEqual(result.compensated_nodes, ("first",))
        self.assertEqual([call[0] for call in executor.calls], ["tool:first", "tool:second", "tool:undo-first"])

    async def test_vm_rejects_caller_only_approval_without_approved_lease(self):
        executor = _Executor()
        vm = CapabilityVm(executor=executor, evidence=self.evidence, clock_ms=lambda: 1_000_001)
        compiled = replace(self._compiled(), requires_human_approval=True)
        with self.assertRaises(CapabilityVmError):
            await vm.execute(
                task_id="task-1",
                capability_digest=self.digest,
                compiled=compiled,
                lease=self.lease,
                inputs={},
                approval_id="invented-approval",
            )
        self.assertEqual(executor.calls, [])

    async def test_vm_rejects_wrong_task_or_artifact_lease_before_action(self):
        executor = _Executor()
        vm = CapabilityVm(executor=executor, evidence=self.evidence, clock_ms=lambda: 1_000_001)
        with self.assertRaises(CapabilityVmError):
            await vm.execute(
                task_id="task-other",
                capability_digest=self.digest,
                compiled=self._compiled(),
                lease=self.lease,
                inputs={},
            )
        with self.assertRaises(CapabilityVmError):
            await vm.execute(
                task_id="task-1",
                capability_digest="sha256:" + "0" * 64,
                compiled=self._compiled(),
                lease=self.lease,
                inputs={},
            )
        self.assertEqual(executor.calls, [])


if __name__ == "__main__":
    unittest.main()
