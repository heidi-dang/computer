import unittest

from cptr.env import (
    CAPABILITY_OS_BUILD_MAX_CPU_MILLIS,
    CAPABILITY_OS_BUILD_MAX_DISK_MIB,
    CAPABILITY_OS_BUILD_MAX_MEMORY_MIB,
    CAPABILITY_OS_BUILD_MAX_OUTPUT_BYTES,
    CAPABILITY_OS_BUILD_MAX_PIDS,
    CAPABILITY_OS_BUILD_MAX_WALL_TIME_MS,
    CAPABILITY_OS_SANDBOX_SOCKET,
)
from cptr.routers.capability_os import (
    _default_credential_broker,
    _default_policy_provider,
    _default_tool_builder,
)
from cptr.routers.gateway import ALLOWED_CONTROL_SCOPES, DEFAULT_CONTROL_SCOPES, OPTIONAL_CONTROL_SCOPES
from cptr.services.capability_os.contracts import CapabilityRequest
from cptr.services.capability_os.credential_broker import ConfigCredentialProvider


class CapabilityOsRouterDefaultsTests(unittest.IsolatedAsyncioTestCase):
    def test_capability_control_scopes_are_allowed_but_never_granted_by_default(self):
        expected = {"capability:read", "capability:write", "capability:execute"}
        self.assertTrue(expected <= set(ALLOWED_CONTROL_SCOPES))
        self.assertTrue(expected <= set(OPTIONAL_CONTROL_SCOPES))
        self.assertTrue(expected.isdisjoint(DEFAULT_CONTROL_SCOPES))

    def test_default_builder_targets_configured_root_broker_socket(self):
        builder = _default_tool_builder()
        self.assertEqual(builder.client.socket_path, CAPABILITY_OS_SANDBOX_SOCKET)
        self.assertEqual(builder.client.expected_uid, 0)

    def test_default_credential_broker_uses_encrypted_server_owned_provider(self):
        broker = _default_credential_broker(clock_ms=lambda: 123)
        self.assertIsInstance(broker._provider, ConfigCredentialProvider)
        self.assertEqual(broker._clock_ms(), 123)

    async def test_default_policy_grants_only_network_denied_exact_build_authority(self):
        async def no_config(_key):
            return None

        provider = _default_policy_provider(config_getter=no_config)
        task = type("Task", (), {"user_id": "u", "workspace_id": "w", "source": "workbench"})()
        digest = "sha256:" + "1" * 64
        policy = await provider.resolve(task=task, artifact_digest=digest, workload_id="tool-build:probe")
        self.assertEqual(policy.allowed, (CapabilityRequest("runtime.build", f"artifact:{digest}"),))
        self.assertEqual(policy.outbound_network, "deny")
        self.assertEqual(policy.max_calls, 1)
        self.assertEqual(policy.resource_limits, {
            "cpuMillis": CAPABILITY_OS_BUILD_MAX_CPU_MILLIS,
            "memoryMiB": CAPABILITY_OS_BUILD_MAX_MEMORY_MIB,
            "diskMiB": CAPABILITY_OS_BUILD_MAX_DISK_MIB,
            "pids": CAPABILITY_OS_BUILD_MAX_PIDS,
            "wallTimeMs": CAPABILITY_OS_BUILD_MAX_WALL_TIME_MS,
            "maxOutputBytes": CAPABILITY_OS_BUILD_MAX_OUTPUT_BYTES,
        })
        self.assertIsNone(await provider.resolve(
            task=task, artifact_digest=digest, workload_id="capability:anything"
        ))


if __name__ == "__main__":
    unittest.main()
