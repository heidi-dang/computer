import unittest

from cptr.services.capability_os.compiler import (
    CapabilityCompileError,
    CapabilityCompiler,
    CapabilitySpec,
    DagEdge,
    DagNode,
    RetryPolicy,
)
from cptr.services.capability_os.contracts import CapabilityRequest


class CapabilityOsCompilerTests(unittest.TestCase):
    def setUp(self):
        self.compiler = CapabilityCompiler()

    def test_compiler_produces_stable_topological_order_and_permission_union(self):
        spec = CapabilitySpec(
            capability_id="deploy.backend.immutable",
            version="1",
            inputs_schema={"type": "object"},
            preconditions=(),
            effects=(CapabilityRequest("production.deploy", "service:cptr-backend"),),
            nodes=(
                DagNode(
                    id="verify",
                    action_ref="tool:health.verify",
                    version="1",
                    permissions=(CapabilityRequest("service.read", "service:cptr-backend"),),
                ),
                DagNode(
                    id="deploy",
                    action_ref="tool:release.switch",
                    version="1",
                    permissions=(CapabilityRequest("production.deploy", "service:cptr-backend"),),
                    approval="human",
                    compensation_action_ref="tool:release.rollback",
                ),
            ),
            edges=(DagEdge("deploy", "verify"),),
            verifiers=("verify",),
            rollback_mode="compensating",
            permissions=(
                CapabilityRequest("service.read", "service:cptr-backend"),
                CapabilityRequest("production.deploy", "service:cptr-backend"),
            ),
            deadline_ms=60_000,
            max_parallelism=1,
            risk_class="high-impact",
        )
        compiled = self.compiler.compile(spec)
        self.assertEqual(compiled.topological_order, ("deploy", "verify"))
        self.assertTrue(compiled.requires_human_approval)
        self.assertEqual({item.action for item in compiled.permissions}, {"service.read", "production.deploy"})

    def test_cycle_unknown_edge_and_permission_escalation_fail_closed(self):
        base_node = DagNode(
            id="a",
            action_ref="tool:a",
            version="1",
            permissions=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
        )
        with self.assertRaisesRegex(CapabilityCompileError, "cycle"):
            self.compiler.compile(
                CapabilitySpec(
                    capability_id="bad.cycle",
                    version="1",
                    inputs_schema={},
                    preconditions=(),
                    effects=(),
                    nodes=(base_node, DagNode(id="b", action_ref="tool:b", version="1")),
                    edges=(DagEdge("a", "b"), DagEdge("b", "a")),
                    verifiers=(),
                    rollback_mode="full",
                    permissions=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
                    deadline_ms=1000,
                    max_parallelism=1,
                    risk_class="read",
                )
            )
        with self.assertRaisesRegex(CapabilityCompileError, "unknown node"):
            self.compiler.compile(
                CapabilitySpec(
                    capability_id="bad.edge",
                    version="1",
                    inputs_schema={},
                    preconditions=(),
                    effects=(),
                    nodes=(base_node,),
                    edges=(DagEdge("a", "missing"),),
                    verifiers=(),
                    rollback_mode="full",
                    permissions=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
                    deadline_ms=1000,
                    max_parallelism=1,
                    risk_class="read",
                )
            )
        with self.assertRaisesRegex(CapabilityCompileError, "permission"):
            self.compiler.compile(
                CapabilitySpec(
                    capability_id="bad.permission",
                    version="1",
                    inputs_schema={},
                    preconditions=(),
                    effects=(),
                    nodes=(base_node,),
                    edges=(),
                    verifiers=(),
                    rollback_mode="full",
                    permissions=(),
                    deadline_ms=1000,
                    max_parallelism=1,
                    risk_class="read",
                )
            )

    def test_retry_requires_idempotent_node_and_high_impact_needs_compensation(self):
        with self.assertRaisesRegex(CapabilityCompileError, "idempotent"):
            self.compiler.compile(
                CapabilitySpec(
                    capability_id="bad.retry",
                    version="1",
                    inputs_schema={},
                    preconditions=(),
                    effects=(),
                    nodes=(
                        DagNode(
                            id="write",
                            action_ref="tool:write",
                            version="1",
                            permissions=(CapabilityRequest("filesystem.write", "repo:cptr/**"),),
                            retry=RetryPolicy(max_attempts=2, only_if_idempotent=True),
                            idempotent=False,
                        ),
                    ),
                    edges=(),
                    verifiers=(),
                    rollback_mode="full",
                    permissions=(CapabilityRequest("filesystem.write", "repo:cptr/**"),),
                    deadline_ms=1000,
                    max_parallelism=1,
                    risk_class="reversible-write",
                )
            )
        with self.assertRaisesRegex(CapabilityCompileError, "rollback"):
            self.compiler.compile(
                CapabilitySpec(
                    capability_id="bad.rollback",
                    version="1",
                    inputs_schema={},
                    preconditions=(),
                    effects=(CapabilityRequest("production.deploy", "service:cptr-backend"),),
                    nodes=(
                        DagNode(
                            id="deploy",
                            action_ref="tool:deploy",
                            version="1",
                            permissions=(CapabilityRequest("production.deploy", "service:cptr-backend"),),
                        ),
                    ),
                    edges=(),
                    verifiers=(),
                    rollback_mode="compensating",
                    permissions=(CapabilityRequest("production.deploy", "service:cptr-backend"),),
                    deadline_ms=1000,
                    max_parallelism=1,
                    risk_class="high-impact",
                )
            )


if __name__ == "__main__":
    unittest.main()
