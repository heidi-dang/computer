import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.capability_os.authority import AuthorityBroker, LeaseRequest, TaskAuthorityPolicy
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
from cptr.services.capability_os.mcp_fabric import AcquisitionGoal, McpFabric, McpQualification
from cptr.services.capability_os.mcp_remote import (
    ConfigRemoteMcpAuthProvider,
    McpAcquisitionService,
    ProjectedMcpActionExecutor,
    RemoteMcpError,
    RemoteMcpObservation,
    RemoteMcpTool,
    StreamableHttpMcpConnector,
)
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.services.capability_os.vm import ActionResult
from cptr.services.factory_discovery import DiscoveryCandidate, FactoryDiscovery


REMOTE_URL = "https://mcp.example.test/mcp"
SERVER_ID = "io.example/logs"
LOGICAL_NAME = "mcp.oauth.logs"
CONSUMER = f"mcp.remote:{REMOTE_URL}"


class _RegistryProvider:
    name = "mcp_registry"

    async def discover(self, query: str, *, limit: int):
        del query
        return [_remote_candidate()][:limit]


class _CredentialProvider:
    def __init__(self, secret: str = "server-owned-token") -> None:
        self.secret = secret
        self.fetches = []

    async def allows(self, *, logical_name: str, consumer: str) -> bool:
        return logical_name == LOGICAL_NAME and consumer == CONSUMER

    async def fetch(self, *, logical_name: str, task_id: str, lease_id: str, consumer: str):
        self.fetches.append((logical_name, task_id, lease_id, consumer))
        return self.secret


class _ExactEndpointValidator:
    async def validate(self, raw_url: str) -> str:
        if raw_url != REMOTE_URL:
            raise RemoteMcpError("unexpected test endpoint")
        return REMOTE_URL


class _CredentialAwareConnector(StreamableHttpMcpConnector):
    def __init__(self, observation: RemoteMcpObservation) -> None:
        super().__init__(endpoint_validator=_ExactEndpointValidator(), timeout_seconds=1.0)
        self.observation = observation
        self.probe_authorizations = []
        self.invoke_authorizations = []

    async def _probe_with_client(self, url, http_client):
        self.probe_authorizations.append(http_client.headers.get("Authorization"))
        return self.observation

    async def _invoke_with_client(self, *, url, tool_name, arguments, http_client):
        self.invoke_authorizations.append(http_client.headers.get("Authorization"))
        return ActionResult(
            output={"tool": tool_name, "arguments": dict(arguments)},
            verification_passed=True,
        )


class _NoProbeConnector:
    async def validate_url(self, remote_url: str) -> str:
        return str(remote_url)

    async def probe(self, remote_url: str):
        raise AssertionError(f"credential-required discovery must not probe {remote_url}")


class _BaseExecutor:
    async def invoke(self, *, action_ref, version, inputs, lease, timeout_ms):
        del version, inputs, lease, timeout_ms
        return ActionResult(output={"base": action_ref})


def _remote_candidate() -> DiscoveryCandidate:
    return DiscoveryCandidate.create(
        provider="mcp_registry",
        candidate_type="mcp_server",
        name=SERVER_ID,
        version="2.1.0",
        origin_uri="https://github.com/example/logs-mcp",
        pinned_version_or_commit="2.1.0",
        capabilities=("mcp-server", "remote-mcp", "tool-provider"),
        permissions=("network:http",),
        metadata={"remotes": [{"url": REMOTE_URL, "type": "streamable-http"}]},
    )


def _goal() -> AcquisitionGoal:
    return AcquisitionGoal(
        task_id="task-1",
        goal="read service logs",
        required=("resource.logs",),
        optional=(),
        forbidden=("resource.delete",),
        data_classification="private",
    )


async def _auth_config(_key: str):
    return [
        {
            "serverId": SERVER_ID,
            "remoteUrl": REMOTE_URL,
            "logicalName": LOGICAL_NAME,
            "mechanism": "bearer",
        }
    ]


class CapabilityOsRemoteMcpAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=sessions)
        self.clock = lambda: 1_000_000
        self.authority = AuthorityBroker(store=self.store, clock_ms=self.clock)
        self.fabric = McpFabric(store=self.store, authority=self.authority, clock_ms=self.clock)
        self.observation = RemoteMcpObservation(
            remote_url=REMOTE_URL,
            protocol_version="2025-06-18",
            server_name="example-logs",
            server_version="2.1.0",
            tools=(
                RemoteMcpTool("resource.logs", digest_payload({"type": "object"})),
                RemoteMcpTool("resource.delete", digest_payload({"type": "object"})),
            ),
        )
        self.credential_provider = _CredentialProvider()
        self.credential_broker = CredentialBroker(
            provider=self.credential_provider,
            clock_ms=self.clock,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _credential_lease(self, *, artifact_digest: str, workload_id: str):
        permission = CapabilityRequest("mcp.invoke", f"mcp:{SERVER_ID}/*")
        policy = TaskAuthorityPolicy(
            allowed=(permission,),
            max_lease_ms=20_000,
            outbound_network="allow-list",
            network_destinations=(REMOTE_URL,),
            credential_names=(LOGICAL_NAME,),
        )
        return await self.authority.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id=workload_id,
                artifact_digest=artifact_digest,
                permissions=(permission,),
                runtime_profile="remote-mcp",
                requested_lease_ms=10_000,
                network_destinations=(REMOTE_URL,),
                credential_names=(LOGICAL_NAME,),
            ),
            policy=policy,
        )

    async def test_server_owned_auth_binding_is_exact_and_fail_closed(self):
        provider = ConfigRemoteMcpAuthProvider(config_getter=_auth_config)
        binding = await provider.resolve(server_id=SERVER_ID, remote_url=REMOTE_URL)
        self.assertEqual(binding.logical_name, LOGICAL_NAME)
        self.assertEqual(binding.consumer, CONSUMER)
        self.assertIsNone(await provider.resolve(server_id="other", remote_url=REMOTE_URL))

        async def duplicate(_key: str):
            row = (await _auth_config(_key))[0]
            return [row, dict(row)]

        with self.assertRaises(RemoteMcpError):
            await ConfigRemoteMcpAuthProvider(config_getter=duplicate).resolve(
                server_id=SERVER_ID,
                remote_url=REMOTE_URL,
            )

    async def test_connector_injects_bearer_only_through_credential_broker(self):
        connector = _CredentialAwareConnector(self.observation)
        artifact = create_artifact(
            artifact_id="mcp.adapter.auth",
            version="1",
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec={"test": True},
            created_at="1970-01-01T00:16:40Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.EPHEMERAL,
        )
        await self.store.persist_artifact(artifact)
        lease = await self._credential_lease(
            artifact_digest=artifact.metadata.content_digest,
            workload_id="mcp-qualify:test",
        )
        observation = await connector.probe(
            REMOTE_URL,
            credential_broker=self.credential_broker,
            credential_lease=lease,
            credential_name=LOGICAL_NAME,
        )
        self.assertEqual(observation.server_name, "example-logs")
        self.assertEqual(connector.probe_authorizations, ["Bearer server-owned-token"])
        self.assertEqual(len(self.credential_provider.fetches), 1)
        with self.assertRaises(RemoteMcpError):
            await connector.probe(REMOTE_URL, credential_name=LOGICAL_NAME)

    async def test_authenticated_remote_discovery_defers_live_probe_until_credential_lease(self):
        acquisition = McpAcquisitionService(
            store=self.store,
            fabric=self.fabric,
            discovery=FactoryDiscovery(providers=(_RegistryProvider(),)),
            connector=_NoProbeConnector(),
            auth_provider=ConfigRemoteMcpAuthProvider(config_getter=_auth_config),
            credential_broker=self.credential_broker,
            clock_ms=self.clock,
        )
        result = await acquisition.discover_and_qualify(
            user_id="user-1",
            task_id="task-1",
            goal=_goal(),
            query="logs",
        )
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].eligible)
        self.assertEqual(result[0].reasons, ("remote-authentication-required",))
        row = await self.store.get_artifact(result[0].artifact_digest)
        self.assertEqual(row.state, ArtifactState.EPHEMERAL.value)
        self.assertEqual(row.spec["authentication"]["logicalName"], LOGICAL_NAME)
        self.assertEqual(row.spec["authentication"]["consumer"], CONSUMER)
        self.assertNotIn("token", row.spec["authentication"])
        self.assertNotIn("headers", row.spec)

    async def test_authenticated_remote_qualification_mount_and_invoke_revalidate_credentials(self):
        connector = _CredentialAwareConnector(self.observation)
        acquisition = McpAcquisitionService(
            store=self.store,
            fabric=self.fabric,
            discovery=FactoryDiscovery(providers=(_RegistryProvider(),)),
            connector=connector,
            auth_provider=ConfigRemoteMcpAuthProvider(config_getter=_auth_config),
            credential_broker=self.credential_broker,
            clock_ms=self.clock,
        )
        discovered = await acquisition.discover_and_qualify(
            user_id="user-1",
            task_id="task-1",
            goal=_goal(),
            query="logs",
        )
        pending = await self.store.get_artifact(discovered[0].artifact_digest)
        qualification_lease = await self._credential_lease(
            artifact_digest=pending.content_digest,
            workload_id="mcp-qualify:logs",
        )
        qualified = await acquisition.qualify_authenticated_remote(
            pending,
            goal=_goal(),
            lease=qualification_lease,
        )
        await self.store.set_artifact_state(
            qualified.artifact_digest,
            state=ArtifactState.QUALIFIED.value,
        )
        row = await self.store.get_artifact(qualified.artifact_digest)
        mount_lease = await self._credential_lease(
            artifact_digest=row.content_digest,
            workload_id="mcp:logs",
        )
        candidate, remote_url = await acquisition.require_mount_candidate(
            row,
            goal=_goal(),
            lease=mount_lease,
        )
        self.assertEqual(remote_url, REMOTE_URL)
        mount = await self.fabric.mount(
            goal=_goal(),
            qualification=McpQualification(candidate=candidate, eligible=True, reasons=()),
            lease=mount_lease,
        )

        capability = create_artifact(
            artifact_id="capability.logs.auth",
            version="1",
            kind=ArtifactKind.CAPABILITY,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.LEARNED,
            spec={"test": True},
            created_at="1970-01-01T00:16:40Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(capability)
        permission = CapabilityRequest("mcp.invoke", f"mcp:{SERVER_ID}/*")
        vm_lease = await self.authority.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="capability:logs",
                artifact_digest=capability.metadata.content_digest,
                permissions=(permission,),
                runtime_profile="cptr-vm",
                requested_lease_ms=10_000,
            ),
            policy=TaskAuthorityPolicy(allowed=(permission,), max_lease_ms=20_000),
        )
        executor = ProjectedMcpActionExecutor(
            base_executor=_BaseExecutor(),
            store=self.store,
            authority=self.authority,
            connector=connector,
            credential_broker=self.credential_broker,
        )
        result = await executor.invoke(
            action_ref=f"mcp://{mount.mount_id}/resource.logs",
            version="2.1.0",
            inputs={"service": "api"},
            lease=vm_lease,
            timeout_ms=1000,
        )
        self.assertEqual(result.output["tool"], "resource.logs")
        self.assertEqual(connector.invoke_authorizations, ["Bearer server-owned-token"])
        self.assertGreaterEqual(len(connector.probe_authorizations), 2)

        await self.authority.revoke(mount_lease.lease_id)
        with self.assertRaises(PermissionError):
            await executor.invoke(
                action_ref=f"mcp://{mount.mount_id}/resource.logs",
                version="2.1.0",
                inputs={},
                lease=vm_lease,
                timeout_ms=1000,
            )


if __name__ == "__main__":
    unittest.main()
