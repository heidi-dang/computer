import unittest

from cptr.services.capability_os.authority import CapabilityLease
from cptr.services.capability_os.credential_broker import (
    ConfigCredentialProvider,
    CredentialBroker,
    CredentialDenied,
    CredentialUnavailable,
)
from cptr.utils.crypto import encrypt_key


class _Provider:
    def __init__(self):
        self.calls = []

    async def fetch(self, *, logical_name, task_id, lease_id, consumer):
        self.calls.append((logical_name, task_id, lease_id, consumer))
        if logical_name == "missing":
            return None
        return "super-secret-value"


def _lease(*, lease_id="lease-1", task_id="task-1", names=("github-production",), expires=10_000):
    return CapabilityLease(
        lease_id=lease_id,
        task_id=task_id,
        workload_id="capability:test",
        artifact_digest="sha256:" + "1" * 64,
        permissions=(),
        resource_limits={},
        network={"outbound": "deny", "destinations": []},
        credentials={"logicalNames": list(names)},
        approval_id="approval-1",
        parent_lease_id=None,
        policy_decision_id="policy-1",
        runtime_profile="cptr-vm",
        issued_at_ms=1_000,
        expires_at_ms=expires,
        status="active",
    )


class ConfigCredentialProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_ai_connection_is_injected_only_into_exact_consumer(self):
        secret_key = "server-test-secret"
        config = {
            "capability_os.credential_sources": [{
                "logicalName": "provider-primary",
                "sourceType": "ai_connection",
                "sourceRef": "conn-1",
                "consumers": ["provider.openai.responses"],
            }],
            "chat.connections": [{
                "id": "conn-1",
                "enabled": True,
                "api_key": encrypt_key("provider-secret", secret_key),
            }],
        }
        reads = []

        async def get_config(key):
            reads.append(key)
            return config.get(key)

        provider = ConfigCredentialProvider(
            config_getter=get_config,
            secret_getter=lambda: secret_key,
        )
        broker = CredentialBroker(provider=provider, clock_ms=lambda: 2_000)
        lease = _lease(names=("provider-primary",))
        handle = await broker.issue(
            lease=lease,
            logical_name="provider-primary",
            consumer="provider.openai.responses",
            ttl_ms=1000,
        )
        captured = []
        result = await broker.use(
            handle_id=handle.handle_id,
            lease=lease,
            consumer="provider.openai.responses",
            operation=lambda value: captured.append(value) or {"ok": True},
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured, ["provider-secret"])
        self.assertNotIn("provider-secret", repr(handle))
        self.assertIn("chat.connections", reads)

        with self.assertRaises(CredentialDenied):
            await broker.issue(
                lease=lease,
                logical_name="provider-primary",
                consumer="provider.openai.chat",
                ttl_ms=1000,
            )

    async def test_plaintext_legacy_and_arbitrary_config_sources_fail_closed(self):
        config = {
            "capability_os.credential_sources": [
                {"logicalName": "legacy", "sourceType": "ai_connection", "sourceRef": "conn-legacy",
                 "consumers": ["provider.openai.responses"]},
                {"logicalName": "arbitrary", "sourceType": "encrypted_config", "sourceRef": "server.secret",
                 "consumers": ["test"]},
            ],
            "chat.connections": [{"id": "conn-legacy", "enabled": True, "api_key": "plaintext-secret"}],
            "server.secret": encrypt_key("must-never-resolve", "server-test-secret"),
        }

        async def get_config(key):
            return config.get(key)

        provider = ConfigCredentialProvider(
            config_getter=get_config,
            secret_getter=lambda: "server-test-secret",
        )
        broker = CredentialBroker(provider=provider, clock_ms=lambda: 2_000)
        lease = _lease(names=("legacy", "arbitrary"))
        handle = await broker.issue(
            lease=lease, logical_name="legacy", consumer="provider.openai.responses", ttl_ms=1000
        )
        with self.assertRaisesRegex(CredentialUnavailable, "logical credential is unavailable"):
            await broker.use(
                handle_id=handle.handle_id,
                lease=lease,
                consumer="provider.openai.responses",
                operation=lambda value: value,
            )
        with self.assertRaises(CredentialDenied):
            await broker.issue(lease=lease, logical_name="arbitrary", consumer="test", ttl_ms=1000)

    async def test_approved_encrypted_product_config_source_is_supported(self):
        secret_key = "server-test-secret"
        config = {
            "capability_os.credential_sources": [{
                "logicalName": "image-generation",
                "sourceType": "encrypted_config",
                "sourceRef": "images.generation_api_key",
                "consumers": ["images.generate"],
            }],
            "images.generation_api_key": encrypt_key("image-secret", secret_key),
        }

        async def get_config(key):
            return config.get(key)

        provider = ConfigCredentialProvider(config_getter=get_config, secret_getter=lambda: secret_key)
        broker = CredentialBroker(provider=provider, clock_ms=lambda: 2_000)
        lease = _lease(names=("image-generation",))
        handle = await broker.issue(
            lease=lease, logical_name="image-generation", consumer="images.generate", ttl_ms=1000
        )
        observed = []
        await broker.use(
            handle_id=handle.handle_id,
            lease=lease,
            consumer="images.generate",
            operation=lambda value: observed.append(value),
        )
        self.assertEqual(observed, ["image-secret"])


class CredentialBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 2_000
        self.provider = _Provider()
        self.broker = CredentialBroker(
            provider=self.provider,
            clock_ms=lambda: self.now,
            max_handle_ttl_ms=2_000,
        )

    async def test_handle_contains_metadata_only_and_secret_flows_only_to_consumer(self):
        lease = _lease()
        handle = await self.broker.issue(
            lease=lease,
            logical_name="github-production",
            consumer="git.push",
            ttl_ms=5_000,
        )
        self.assertNotIn("super-secret", repr(handle))
        self.assertLessEqual(handle.expires_at_ms, self.now + 2_000)
        captured = []

        async def consumer(secret):
            captured.append(secret)
            return {"ok": True, "length": len(secret)}

        result = await self.broker.use(
            handle_id=handle.handle_id,
            lease=lease,
            consumer="git.push",
            operation=consumer,
        )
        self.assertEqual(result, {"ok": True, "length": len("super-secret-value")})
        self.assertEqual(captured, ["super-secret-value"])
        state = await self.broker.inspect(handle.handle_id)
        self.assertEqual(state.status, "consumed")
        self.assertEqual(state.uses, 1)
        with self.assertRaises(CredentialDenied):
            await self.broker.use(
                handle_id=handle.handle_id,
                lease=lease,
                consumer="git.push",
                operation=lambda secret: None,
            )

    async def test_logical_name_consumer_and_lease_binding_fail_closed(self):
        lease = _lease()
        with self.assertRaises(CredentialDenied):
            await self.broker.issue(
                lease=lease, logical_name="aws-production", consumer="deploy", ttl_ms=1000
            )
        handle = await self.broker.issue(
            lease=lease, logical_name="github-production", consumer="git.push", ttl_ms=1000
        )
        with self.assertRaises(CredentialDenied):
            await self.broker.use(
                handle_id=handle.handle_id,
                lease=lease,
                consumer="mcp.remote",
                operation=lambda secret: None,
            )
        other = _lease(lease_id="lease-2")
        with self.assertRaises(CredentialDenied):
            await self.broker.use(
                handle_id=handle.handle_id,
                lease=other,
                consumer="git.push",
                operation=lambda secret: None,
            )
        self.assertEqual(self.provider.calls, [])

    async def test_expiry_and_task_revocation_remove_usable_authority(self):
        lease = _lease()
        first = await self.broker.issue(
            lease=lease, logical_name="github-production", consumer="git.push", ttl_ms=500
        )
        second = await self.broker.issue(
            lease=lease, logical_name="github-production", consumer="mcp.remote", ttl_ms=1000
        )
        self.now = 2_600
        with self.assertRaises(CredentialDenied):
            await self.broker.use(
                handle_id=first.handle_id,
                lease=lease,
                consumer="git.push",
                operation=lambda secret: None,
            )
        self.now = 2_700
        self.assertEqual(await self.broker.revoke_task("task-1"), 1)
        with self.assertRaises(CredentialDenied):
            await self.broker.use(
                handle_id=second.handle_id,
                lease=lease,
                consumer="mcp.remote",
                operation=lambda secret: None,
            )

    async def test_inject_is_one_shot_callback_only_and_leaves_no_active_handle(self):
        lease = _lease()
        seen = []
        result = await self.broker.inject(
            lease=lease,
            logical_name="github-production",
            consumer="git.push",
            ttl_ms=1000,
            operation=lambda secret: seen.append(secret) or "ok",
        )
        self.assertEqual(result, "ok")
        self.assertEqual(seen, ["super-secret-value"])
        active = [state for state in self.broker._handles.values() if state.status == "active"]
        self.assertEqual(active, [])

    async def test_stale_lease_snapshot_cannot_bypass_durable_lease_revocation(self):
        durable_active = True
        validations = []

        async def validate(lease):
            validations.append(lease.lease_id)
            return durable_active

        broker = CredentialBroker(
            provider=self.provider,
            clock_ms=lambda: self.now,
            lease_validator=validate,
        )
        lease = _lease()
        handle = await broker.issue(
            lease=lease,
            logical_name="github-production",
            consumer="git.push",
            ttl_ms=1000,
        )
        durable_active = False
        with self.assertRaisesRegex(CredentialDenied, "lease is no longer active"):
            await broker.use(
                handle_id=handle.handle_id,
                lease=lease,
                consumer="git.push",
                operation=lambda secret: None,
            )
        self.assertEqual(validations, [lease.lease_id, lease.lease_id])
        self.assertEqual(self.provider.calls, [])

    async def test_missing_provider_secret_is_generic_and_consumes_one_shot_handle(self):
        lease = _lease(names=("missing",))
        handle = await self.broker.issue(
            lease=lease, logical_name="missing", consumer="test", ttl_ms=1000
        )
        with self.assertRaisesRegex(CredentialUnavailable, "logical credential is unavailable"):
            await self.broker.use(
                handle_id=handle.handle_id,
                lease=lease,
                consumer="test",
                operation=lambda secret: None,
            )
        state = await self.broker.inspect(handle.handle_id)
        self.assertEqual(state.status, "consumed")


if __name__ == "__main__":
    unittest.main()
