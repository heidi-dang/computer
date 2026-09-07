import unittest
from types import SimpleNamespace

from cptr.services.capability_os.authority import AuthorityDenied, TaskAuthorityPolicy
from cptr.services.capability_os.contracts import CapabilityRequest
from cptr.services.capability_os.policy import (
    CompositeAuthorityPolicyProvider,
    ConfigStandingAuthorityPolicyProvider,
    DenyAllAuthorityPolicyProvider,
    StandingAuthorityPolicyProvider,
    StandingAuthorityRule,
    SafeIsolationAuthorityPolicyProvider,
)


class CapabilityOsPolicyTests(unittest.IsolatedAsyncioTestCase):
    def task(self, *, user="u1", workspace="w1", source="workbench"):
        return SimpleNamespace(user_id=user, workspace_id=workspace, source=source)

    async def test_default_provider_denies_everything(self):
        provider = DenyAllAuthorityPolicyProvider()
        self.assertIsNone(await provider.resolve(task=self.task(), artifact_digest="sha256:x", workload_id="x"))

    async def test_most_specific_server_rule_wins(self):
        broad = TaskAuthorityPolicy(allowed=(CapabilityRequest("filesystem.read", "repo:public/**"),))
        exact = TaskAuthorityPolicy(allowed=(CapabilityRequest("filesystem.read", "repo:cptr/**"),))
        provider = StandingAuthorityPolicyProvider((
            StandingAuthorityRule(policy=broad, user_id="u1"),
            StandingAuthorityRule(policy=exact, user_id="u1", workspace_id="w1"),
        ))
        selected = await provider.resolve(task=self.task(), artifact_digest="sha256:x", workload_id="x")
        self.assertEqual(selected, exact)

    async def test_safe_isolation_policy_only_grants_exact_artifact_build_with_bounded_resources(self):
        provider = SafeIsolationAuthorityPolicyProvider(
            max_cpu_millis=2000, max_memory_mib=256, max_disk_mib=128, max_pids=32,
            max_wall_time_ms=30000, max_output_bytes=1048576,
        )
        policy = await provider.resolve(
            task=self.task(), artifact_digest="sha256:" + "1" * 64, workload_id="tool-build:probe"
        )
        self.assertEqual(policy.allowed[0], CapabilityRequest("runtime.build", "artifact:sha256:" + "1" * 64))
        self.assertEqual(policy.outbound_network, "deny")
        self.assertEqual(policy.resource_limits["memoryMiB"], 256)
        self.assertIsNone(await provider.resolve(
            task=self.task(), artifact_digest="sha256:" + "1" * 64, workload_id="capability:deploy"
        ))

    async def test_durable_config_policy_is_selector_and_workload_bound(self):
        raw = [{
            "enabled": True,
            "selectors": {
                "userId": "u1",
                "workspaceId": "w1",
                "taskSource": "workbench",
                "workloadPattern": "capability:read-*",
                "artifactPattern": "sha256:*",
            },
            "policy": {
                "allowed": [{"action": "filesystem.read", "resource": "repo:cptr/**"}],
                "forbidden": [{"action": "filesystem.read", "resource": "repo:cptr/secrets/**"}],
                "maxLeaseMs": 5000,
                "maxCalls": 3,
                "outboundNetwork": "deny",
                "resourceLimits": {"maxOutputBytes": 65536},
            },
        }]

        async def get_config(key):
            self.assertEqual(key, "capability_os.authority_policies")
            return raw

        provider = ConfigStandingAuthorityPolicyProvider(config_getter=get_config)
        selected = await provider.resolve(
            task=self.task(),
            artifact_digest="sha256:" + "1" * 64,
            workload_id="capability:read-repo",
        )
        self.assertEqual(selected.max_lease_ms, 5000)
        self.assertEqual(selected.max_calls, 3)
        self.assertEqual(
            selected.allowed,
            (CapabilityRequest("filesystem.read", "repo:cptr/**"),),
        )
        self.assertIsNone(await provider.resolve(
            task=self.task(),
            artifact_digest="sha256:" + "1" * 64,
            workload_id="capability:write-repo",
        ))
        self.assertIsNone(await provider.resolve(
            task=self.task(user="other"),
            artifact_digest="sha256:" + "1" * 64,
            workload_id="capability:read-repo",
        ))

    async def test_invalid_config_and_cross_provider_overlap_fail_closed(self):
        async def invalid(_key):
            return [{
                "selectors": {"unknownSelector": "*"},
                "policy": {"allowed": [{"action": "filesystem.read", "resource": "repo:**"}]},
            }]

        provider = ConfigStandingAuthorityPolicyProvider(config_getter=invalid)
        with self.assertRaisesRegex(AuthorityDenied, "configuration is invalid"):
            await provider.resolve(
                task=self.task(), artifact_digest="sha256:x", workload_id="capability:test"
            )

        policy = TaskAuthorityPolicy(
            allowed=(CapabilityRequest("runtime.build", "artifact:sha256:*"),)
        )
        composite = CompositeAuthorityPolicyProvider((
            StandingAuthorityPolicyProvider((StandingAuthorityRule(policy=policy),)),
            StandingAuthorityPolicyProvider((StandingAuthorityRule(policy=policy),)),
        ))
        with self.assertRaisesRegex(AuthorityDenied, "multiple standing authority providers"):
            await composite.resolve(
                task=self.task(), artifact_digest="sha256:x", workload_id="tool-build:test"
            )

    async def test_equally_specific_matching_rules_fail_closed(self):
        policy = TaskAuthorityPolicy(allowed=(CapabilityRequest("filesystem.read", "repo:cptr/**"),))
        provider = StandingAuthorityPolicyProvider((
            StandingAuthorityRule(policy=policy, user_id="u1"),
            StandingAuthorityRule(policy=policy, workspace_id="w1"),
        ))
        with self.assertRaises(AuthorityDenied):
            await provider.resolve(task=self.task(), artifact_digest="sha256:x", workload_id="x")


if __name__ == "__main__":
    unittest.main()
