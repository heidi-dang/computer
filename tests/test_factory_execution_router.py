import asyncio
import unittest

from cptr.services.factory_capabilities import (
    CapabilityManifest,
    CapabilityTrustStatus,
    CapabilityVerificationStatus,
)
from cptr.services.factory_execution_router import (
    ExecutionPolicy,
    ExecutionProviderAdapter,
    ExecutionRequest,
    FactoryExecutionError,
    FactoryExecutionRouter,
    ProviderExecutionResult,
)


def _manifest(
    requirement: str,
    *,
    name: str = "capability",
    trust: CapabilityTrustStatus = CapabilityTrustStatus.APPROVED,
    permissions=("workspace:read",),
    network_requirements=(),
):
    return CapabilityManifest(
        stable_id=f"cap_{name}_{requirement.replace(':', '_')}",
        version="1",
        origin_type="builtin",
        origin_uri=f"cptr:{name}",
        pinned_version_or_commit="1",
        digest=(name + requirement + "0" * 64)[:64],
        capabilities=("factory-execution",),
        permissions=tuple(permissions),
        network_requirements=tuple(network_requirements),
        execution_requirements=(requirement,),
        risk_classification="CPTR_BUILTIN",
        trust_status=trust,
        verification_status=CapabilityVerificationStatus.LOCAL,
        maintenance_metadata={},
    )


class _Recorder:
    def __init__(self, result=None, delay=0.0):
        self.calls = []
        self.result = result or ProviderExecutionResult(output={"ok": True}, metadata={"source": "fake"})
        self.delay = delay

    async def __call__(self, request):
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


class FactoryExecutionRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.recorders = {
            name: _Recorder()
            for name in ("cptr", "fdx", "lsp", "command", "browser", "mcp", "ssh", "terminal")
        }
        self.router = FactoryExecutionRouter(
            providers={
                name: ExecutionProviderAdapter(name=name, handler=recorder)
                for name, recorder in self.recorders.items()
            }
        )
        self.policy = ExecutionPolicy(
            allowed_permissions=frozenset(
                {"workspace:read", "workspace:write", "process:execute", "network:http"}
            ),
            network_allowed=True,
            allowed_ssh_aliases=frozenset({"prod"}),
            allow_terminal_plugin=True,
            max_output_bytes=4096,
            max_runtime_ms=1000,
        )

    def _request(self, manifest, **overrides):
        values = {
            "run_id": "run-1",
            "cycle_id": "cycle-1",
            "manifest": manifest,
            "operation": "inspect",
            "arguments": {},
            "required_permissions": ("workspace:read",),
            "network_required": False,
        }
        values.update(overrides)
        return ExecutionRequest(**values)

    async def test_every_nonapproved_trust_state_is_rejected_before_provider_invocation(self):
        for trust in (
            CapabilityTrustStatus.DISCOVERED,
            CapabilityTrustStatus.FETCHED,
            CapabilityTrustStatus.PINNED,
            CapabilityTrustStatus.QUARANTINED,
            CapabilityTrustStatus.REJECTED,
            CapabilityTrustStatus.REVOKED,
            CapabilityTrustStatus.STALE_REVIEW_REQUIRED,
        ):
            with self.subTest(trust=trust):
                with self.assertRaises(FactoryExecutionError) as caught:
                    await self.router.execute(
                        self._request(_manifest("cptr-direct-coding", trust=trust)),
                        self.policy,
                    )
                self.assertEqual(caught.exception.code, "FACTORY_EXECUTION_TRUST_REJECTED")
        self.assertEqual(self.recorders["cptr"].calls, [])

    async def test_manifest_and_request_permission_rejections_happen_before_provider_call(self):
        restrictive = ExecutionPolicy(
            allowed_permissions=frozenset({"workspace:read"}),
            network_allowed=False,
            max_output_bytes=1024,
            max_runtime_ms=1000,
        )
        writer = _manifest(
            "cptr-direct-coding",
            permissions=("workspace:read", "workspace:write"),
        )
        with self.assertRaises(FactoryExecutionError) as manifest_reject:
            await self.router.execute(self._request(writer), restrictive)
        self.assertEqual(manifest_reject.exception.code, "FACTORY_EXECUTION_PERMISSION_REJECTED")

        reader = _manifest("cptr-direct-coding", permissions=("workspace:read",))
        with self.assertRaises(FactoryExecutionError) as request_reject:
            await self.router.execute(
                self._request(reader, required_permissions=("workspace:write",)),
                self.policy,
            )
        self.assertEqual(request_reject.exception.code, "FACTORY_EXECUTION_PERMISSION_ESCALATION")
        self.assertEqual(self.recorders["cptr"].calls, [])

    async def test_network_rejection_happens_before_provider_call(self):
        manifest = _manifest(
            "managed-browser",
            permissions=("network:http",),
            network_requirements=("external-http",),
        )
        restrictive = ExecutionPolicy(
            allowed_permissions=frozenset({"network:http"}),
            network_allowed=False,
            max_output_bytes=1024,
            max_runtime_ms=1000,
        )
        with self.assertRaises(FactoryExecutionError) as caught:
            await self.router.execute(
                self._request(
                    manifest,
                    required_permissions=("network:http",),
                    network_required=True,
                ),
                restrictive,
            )
        self.assertEqual(caught.exception.code, "FACTORY_EXECUTION_NETWORK_REJECTED")
        self.assertEqual(self.recorders["browser"].calls, [])

    async def test_supported_execution_requirements_route_to_expected_provider(self):
        matrix = {
            "cptr-direct-coding": "cptr",
            "git-service": "cptr",
            "fdx": "fdx",
            "lsp": "lsp",
            "command-service": "command",
            "managed-browser": "browser",
            "web-search": "browser",
            "mcp-client": "mcp",
            "mcp-stdio-client": "mcp",
            "ssh-configured": "ssh",
            "terminal-plugin": "terminal",
        }
        for requirement, provider in matrix.items():
            with self.subTest(requirement=requirement):
                permissions = ("workspace:read",)
                arguments = {}
                required = ("workspace:read",)
                network = False
                network_requirements = ()
                if provider == "command":
                    permissions = ("process:execute", "workspace:read")
                    required = permissions
                elif provider == "browser":
                    permissions = ("network:http",)
                    required = permissions
                    network = True
                    network_requirements = ("external-http",)
                elif provider == "mcp":
                    permissions = ("network:http",) if requirement == "mcp-client" else ("process:execute",)
                    required = permissions
                    network = requirement == "mcp-client"
                    network_requirements = ("external-http",) if network else ()
                elif provider == "ssh":
                    permissions = ("process:execute", "network:http")
                    required = permissions
                    network = True
                    network_requirements = ("external-http",)
                    arguments = {"alias": "prod", "command": "status"}
                result = await self.router.execute(
                    self._request(
                        _manifest(
                            requirement,
                            name=provider,
                            permissions=permissions,
                            network_requirements=network_requirements,
                        ),
                        arguments=arguments,
                        required_permissions=required,
                        network_required=network,
                    ),
                    self.policy,
                )
                self.assertEqual(result.provider, provider)
                self.assertEqual(result.capability_identity.split(":")[0], result.stable_id)
                self.assertTrue(result.evidence_id.startswith("fexec_"))
                self.assertEqual(result.status, "PASS")
        for provider, recorder in self.recorders.items():
            self.assertGreater(len(recorder.calls), 0, provider)

    async def test_unknown_or_ambiguous_requirement_is_rejected_before_any_provider(self):
        unknown = _manifest("unknown-provider")
        with self.assertRaises(FactoryExecutionError) as caught:
            await self.router.execute(self._request(unknown), self.policy)
        self.assertEqual(caught.exception.code, "FACTORY_EXECUTION_PROVIDER_UNAVAILABLE")

        ambiguous = CapabilityManifest(
            **{
                **_manifest("fdx").__dict__,
                "execution_requirements": ("fdx", "lsp"),
            }
        )
        with self.assertRaises(FactoryExecutionError) as ambiguous_error:
            await self.router.execute(self._request(ambiguous), self.policy)
        self.assertEqual(ambiguous_error.exception.code, "FACTORY_EXECUTION_PROVIDER_AMBIGUOUS")
        self.assertEqual(sum(len(item.calls) for item in self.recorders.values()), 0)

    async def test_terminal_plugin_and_ssh_alias_are_explicitly_policy_gated(self):
        terminal_policy = ExecutionPolicy(
            allowed_permissions=frozenset({"workspace:read"}),
            network_allowed=False,
            allow_terminal_plugin=False,
            max_output_bytes=1024,
            max_runtime_ms=1000,
        )
        with self.assertRaises(FactoryExecutionError) as terminal_error:
            await self.router.execute(
                self._request(_manifest("terminal-plugin")),
                terminal_policy,
            )
        self.assertEqual(terminal_error.exception.code, "FACTORY_EXECUTION_TERMINAL_DISABLED")

        ssh = _manifest(
            "ssh-configured",
            permissions=("process:execute", "network:http"),
            network_requirements=("external-http",),
        )
        with self.assertRaises(FactoryExecutionError) as ssh_error:
            await self.router.execute(
                self._request(
                    ssh,
                    arguments={"alias": "other", "command": "status"},
                    required_permissions=("process:execute", "network:http"),
                    network_required=True,
                ),
                self.policy,
            )
        self.assertEqual(ssh_error.exception.code, "FACTORY_EXECUTION_SSH_ALIAS_REJECTED")
        self.assertEqual(self.recorders["terminal"].calls, [])
        self.assertEqual(self.recorders["ssh"].calls, [])

    def test_provider_adapter_rejects_non_async_handler_to_preserve_cancellation_bounds(self):
        with self.assertRaises(TypeError):
            ExecutionProviderAdapter(
                name="cptr",
                handler=lambda _request: ProviderExecutionResult(output={"ok": True}),
            )

    async def test_outputs_are_redacted_bounded_and_digest_normalized(self):
        recorder = _Recorder(
            ProviderExecutionResult(
                output={
                    "message": "Authorization: Bearer abcdefghijklmnop",
                    "payload": "x" * 5000,
                    "token": "do-not-persist",
                },
                metadata={"authorization": "Bearer secret", "source": "machine"},
            )
        )
        router = FactoryExecutionRouter(
            providers={"cptr": ExecutionProviderAdapter(name="cptr", handler=recorder)}
        )
        policy = ExecutionPolicy(
            allowed_permissions=frozenset({"workspace:read"}),
            network_allowed=False,
            max_output_bytes=256,
            max_runtime_ms=1000,
        )

        result = await router.execute(
            self._request(_manifest("cptr-direct-coding")),
            policy,
        )

        self.assertTrue(result.truncated)
        self.assertLessEqual(len(str(result.output).encode("utf-8")), 512)
        self.assertNotIn("do-not-persist", str(result.output))
        self.assertNotIn("abcdefghijklmnop", str(result.output))
        self.assertNotIn("Bearer secret", str(result.metadata))
        self.assertEqual(result.metadata, {"source": "machine"})
        self.assertEqual(len(result.output_digest), 64)

    async def test_provider_timeout_is_bounded_and_normalized(self):
        recorder = _Recorder(delay=0.05)
        router = FactoryExecutionRouter(
            providers={"cptr": ExecutionProviderAdapter(name="cptr", handler=recorder)}
        )
        policy = ExecutionPolicy(
            allowed_permissions=frozenset({"workspace:read"}),
            network_allowed=False,
            max_output_bytes=1024,
            max_runtime_ms=5,
        )

        with self.assertRaises(FactoryExecutionError) as caught:
            await router.execute(
                self._request(_manifest("cptr-direct-coding")),
                policy,
            )

        self.assertEqual(caught.exception.code, "FACTORY_EXECUTION_PROVIDER_TIMEOUT")
        self.assertEqual(len(recorder.calls), 1)


if __name__ == "__main__":
    unittest.main()
