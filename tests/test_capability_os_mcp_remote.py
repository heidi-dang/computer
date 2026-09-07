import hashlib
import tempfile
import unittest
from pathlib import Path

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
from cptr.services.capability_os.mcp_fabric import AcquisitionGoal, McpCandidate, McpFabric, McpQualification
from cptr.services.capability_os.mcp_remote import (
    McpAcquisitionService,
    ProjectedMcpActionExecutor,
    RemoteMcpObservation,
    RemoteMcpTool,
)
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.services.capability_os.vm import ActionResult
from cptr.services.factory_discovery import DiscoveryCandidate, FactoryDiscovery, QuarantineCache


class _Provider:
    name = "mcp_registry"

    def __init__(self, candidates):
        self.candidates = list(candidates)

    async def discover(self, query: str, *, limit: int):
        return self.candidates[:limit]


class _Fetcher:
    def __init__(self, content: bytes):
        self.content = content

    async def fetch(self, candidate, *, max_bytes: int, timeout_ms: int):
        if len(self.content) > max_bytes:
            raise RuntimeError("too large")
        return self.content


class _Connector:
    def __init__(self, observation: RemoteMcpObservation):
        self.observation = observation
        self.probes = []
        self.calls = []

    async def probe(self, remote_url: str):
        self.probes.append(remote_url)
        return self.observation

    async def invoke(self, *, remote_url: str, tool_name: str, arguments: dict):
        self.calls.append((remote_url, tool_name, arguments))
        return ActionResult(output={"tool": tool_name, "arguments": arguments}, verification_passed=True)

    async def release(self, **_kwargs):
        return None


class _BaseExecutor:
    async def invoke(self, *, action_ref, version, inputs, lease, timeout_ms):
        return ActionResult(output={"base": action_ref})


def _remote_candidate(remote_url="https://mcp.example.test/mcp"):
    return DiscoveryCandidate.create(
        provider="mcp_registry",
        candidate_type="mcp_server",
        name="io.example/logs",
        version="2.1.0",
        origin_uri="https://github.com/example/logs-mcp",
        pinned_version_or_commit="2.1.0",
        capabilities=("mcp-server", "remote-mcp", "tool-provider"),
        permissions=("network:http",),
        metadata={"remotes": [{"url": remote_url, "type": "streamable-http"}]},
    )


def _goal(task_id="task-1"):
    return AcquisitionGoal(
        task_id=task_id,
        goal="read service logs",
        required=("resource.logs",),
        optional=("resource.inspect",),
        forbidden=("resource.delete",),
        data_classification="private",
    )


class CapabilityOsRemoteMcpTests(unittest.IsolatedAsyncioTestCase):
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
            remote_url="https://mcp.example.test/mcp",
            protocol_version="2025-06-18",
            server_name="example-logs",
            server_version="2.1.0",
            tools=(
                RemoteMcpTool("resource.logs", digest_payload({"type": "object"})),
                RemoteMcpTool("resource.inspect", digest_payload({"type": "object"})),
                RemoteMcpTool("resource.delete", digest_payload({"type": "object"})),
            ),
        )
        self.connector = _Connector(self.observation)
        self.discovery = FactoryDiscovery(providers=(_Provider([_remote_candidate()]),))
        self.acquisition = McpAcquisitionService(
            store=self.store,
            fabric=self.fabric,
            discovery=self.discovery,
            connector=self.connector,
            clock_ms=self.clock,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_discovery_persists_server_observed_task_user_scoped_adapter(self):
        result = await self.acquisition.discover_and_qualify(
            user_id="user-1", task_id="task-1", goal=_goal(), query="service logs"
        )
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].eligible)
        row = await self.store.get_artifact(
            result[0].artifact_digest, user_id="user-1", include_global=False
        )
        self.assertIsNotNone(row)
        self.assertEqual(row.kind, ArtifactKind.MCP_ADAPTER.value)
        self.assertEqual(row.user_id, "user-1")
        self.assertEqual(row.task_origin, "task-1")
        self.assertEqual(row.state, ArtifactState.QUALIFIED.value)
        self.assertEqual(row.spec["acquisitionTaskId"], "task-1")
        self.assertEqual(row.spec["remote"]["url"], self.observation.remote_url)
        self.assertEqual(
            [item["name"] for item in row.spec["tools"]],
            ["resource.logs", "resource.inspect", "resource.delete"],
        )
        self.assertNotIn("identityOk", row.spec)
        self.assertNotIn("authOk", row.spec)
        self.assertNotIn("sandboxable", row.spec)

    async def test_mount_revalidates_live_identity_and_task_scope(self):
        acquired = await self.acquisition.discover_and_qualify(
            user_id="user-1", task_id="task-1", goal=_goal(), query="service logs"
        )
        row = await self.store.get_artifact(acquired[0].artifact_digest)
        candidate, remote_url = await self.acquisition.require_mount_candidate(row, goal=_goal())
        self.assertEqual(candidate.tools[0], "resource.logs")
        self.assertEqual(remote_url, self.observation.remote_url)

        with self.assertRaises(PermissionError):
            await self.acquisition.require_mount_candidate(row, goal=_goal("task-2"))

        self.connector.observation = RemoteMcpObservation(
            remote_url=self.observation.remote_url,
            protocol_version=self.observation.protocol_version,
            server_name=self.observation.server_name,
            server_version="2.1.1",
            tools=self.observation.tools,
        )
        with self.assertRaises(PermissionError):
            await self.acquisition.require_mount_candidate(row, goal=_goal())

    async def test_packaged_mcp_is_quarantined_not_qualified_or_executed(self):
        payload = b"#!/bin/sh\necho should-never-run\n"
        digest = hashlib.sha256(payload).hexdigest()
        package = DiscoveryCandidate.create(
            provider="mcp_registry",
            candidate_type="mcp_server",
            name="io.example/package-only",
            version="1.0.0",
            origin_uri="https://registry.modelcontextprotocol.io/v0.1/servers/io.example/package-only",
            source_uri="https://registry.npmjs.org/example-package/-/example-package-1.0.0.tgz",
            pinned_version_or_commit="1.0.0",
            expected_digest=digest,
            capabilities=("mcp-server", "packaged-mcp"),
            permissions=("network:http", "process:execute"),
            metadata={"packages": [{"identifier": "example-package"}]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            discovery = FactoryDiscovery(
                providers=(_Provider([package]),),
                artifact_fetcher=_Fetcher(payload),
                quarantine_cache=QuarantineCache(Path(tmp) / "quarantine"),
            )
            acquisition = McpAcquisitionService(
                store=self.store,
                fabric=self.fabric,
                discovery=discovery,
                connector=self.connector,
                clock_ms=self.clock,
            )
            result = await acquisition.discover_and_qualify(
                user_id="user-1", task_id="task-1", goal=_goal(), query="package"
            )
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].eligible)
        self.assertIn("package-quarantined-pending-gvisor", result[0].reasons)
        row = await self.store.get_artifact(result[0].artifact_digest)
        self.assertEqual(row.state, ArtifactState.EPHEMERAL.value)
        self.assertFalse(row.spec["package"]["executable"])
        self.assertEqual(row.spec["package"]["sha256"], digest)

    async def test_projected_executor_requires_both_vm_and_remote_mount_leases(self):
        permission = CapabilityRequest("mcp.invoke", "mcp:io.example/logs/*")
        adapter = create_artifact(
            artifact_id="mcp.adapter.logs",
            version="2.1.0",
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec={
                "serverId": "io.example/logs",
                "acquisitionTaskId": "task-1",
                "remote": {"url": self.observation.remote_url, "transport": "streamable-http"},
                "identityFingerprint": self.observation.fingerprint,
                "tools": [
                    {"name": item.name, "inputSchemaDigest": item.input_schema_digest}
                    for item in self.observation.tools
                ],
                "permissions": [permission.to_dict()],
            },
            created_at="1970-01-01T00:16:40Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(adapter)
        remote_policy = TaskAuthorityPolicy(
            allowed=(permission,),
            max_lease_ms=20_000,
            outbound_network="allow-list",
            network_destinations=(self.observation.remote_url,),
        )
        remote_lease = await self.authority.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="mcp:io.example/logs",
                artifact_digest=adapter.metadata.content_digest,
                permissions=(permission,),
                runtime_profile="remote-mcp",
                requested_lease_ms=10_000,
                network_destinations=(self.observation.remote_url,),
            ),
            policy=remote_policy,
        )
        candidate = McpCandidate(
            server_id="io.example/logs",
            version="2.1.0",
            digest=adapter.metadata.content_digest,
            transport_kind="streamable-http",
            tools=("resource.logs", "resource.inspect", "resource.delete"),
            permissions=(permission,),
            identity_ok=True,
            auth_ok=True,
            sandboxable=True,
            hard_denies=(),
        )
        mount = await self.fabric.mount(
            goal=_goal(),
            qualification=McpQualification(candidate=candidate, eligible=True, reasons=()),
            lease=remote_lease,
        )

        capability = create_artifact(
            artifact_id="capability.logs",
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
            connector=self.connector,
        )
        result = await executor.invoke(
            action_ref=f"mcp://{mount.mount_id}/resource.logs",
            version="2.1.0",
            inputs={"service": "api"},
            lease=vm_lease,
            timeout_ms=1000,
        )
        self.assertEqual(result.output["tool"], "resource.logs")
        self.assertEqual(len(self.connector.calls), 1)

        with self.assertRaises(PermissionError):
            await executor.invoke(
                action_ref=f"mcp://{mount.mount_id}/resource.delete",
                version="2.1.0",
                inputs={},
                lease=vm_lease,
                timeout_ms=1000,
            )

        await self.authority.revoke(remote_lease.lease_id)
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
