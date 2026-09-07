import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.capability_os.authority import AuthorityBroker, LeaseRequest, TaskAuthorityPolicy
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    CapabilityRequest,
    create_artifact,
)
from cptr.services.capability_os.mcp_fabric import (
    AcquisitionGoal,
    AcquisitionState,
    McpCandidate,
    McpFabric,
    McpQualification,
    beta_lower_bound,
)
from cptr.services.capability_os.store import SqlCapabilityOsStore


class _CredentialRevocations:
    def __init__(self):
        self.leases = []
        self.tasks = []

    async def revoke_lease(self, lease_id):
        self.leases.append(lease_id)
        return 1

    async def revoke_task(self, task_id):
        self.tasks.append(task_id)
        return 1


class CapabilityOsMcpFabricTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=self.sessions)
        self.authority = AuthorityBroker(store=self.store, clock_ms=lambda: 1_000_000)
        self.adapter = create_artifact(
            artifact_id="mcp.vendor.example",
            version="2.4.1",
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec={"transport": "streamable-http"},
            created_at="2026-09-07T01:00:00Z",
            task_origin="task-1",
        )
        await self.store.persist_artifact(self.adapter)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_beta_reliability_is_conservative_for_tiny_sample(self):
        one_success = beta_lower_bound(successes=1, failures=0)
        hundred_successes = beta_lower_bound(successes=100, failures=0)
        self.assertLess(one_success, hundred_successes)
        self.assertLess(one_success, 0.9)

    async def test_mount_projects_only_required_tools_and_release_reaches_zero_authority(self):
        candidate = McpCandidate(
            server_id="io.vendor.example",
            version="2.4.1",
            digest=self.adapter.metadata.content_digest,
            transport_kind="streamable-http",
            tools=("resource.inspect", "resource.logs", "resource.delete", "secrets.read"),
            permissions=(CapabilityRequest("resource.read", "vendor:resource/**"),),
            identity_ok=True,
            auth_ok=True,
            sandboxable=True,
            hard_denies=(),
            successes=50,
            failures=2,
        )
        goal = AcquisitionGoal(
            task_id="task-1",
            goal="inspect resource logs",
            required=("resource.inspect", "resource.logs"),
            optional=(),
            forbidden=("resource.delete", "secrets.read"),
            data_classification="private",
        )
        qualification = McpQualification(candidate=candidate, eligible=True, reasons=())
        policy = TaskAuthorityPolicy(
            allowed=(CapabilityRequest("resource.read", "vendor:resource/**"),),
            max_lease_ms=30_000,
        )
        lease = await self.authority.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="mcp:io.vendor.example",
                artifact_digest=self.adapter.metadata.content_digest,
                permissions=(CapabilityRequest("resource.read", "vendor:resource/**"),),
                runtime_profile="remote-mcp",
                requested_lease_ms=10_000,
            ),
            policy=policy,
        )
        credentials = _CredentialRevocations()
        fabric = McpFabric(
            store=self.store,
            authority=self.authority,
            credential_broker=credentials,
        )
        mount = await fabric.mount(goal=goal, qualification=qualification, lease=lease)
        self.assertEqual(mount.state, AcquisitionState.MOUNTED)
        self.assertEqual(mount.projected_tools, ("resource.inspect", "resource.logs"))
        self.assertNotIn("resource.delete", mount.projected_tools)
        self.assertNotIn("secrets.read", mount.projected_tools)
        await fabric.release(mount.mount_id)
        self.assertEqual(credentials.leases, [lease.lease_id])
        await fabric.close_task("task-1")
        self.assertEqual(credentials.tasks, ["task-1"])
        self.assertEqual(await self.store.list_active_mounts("task-1"), [])
        self.assertEqual(await self.store.list_active_leases("task-1", now_ms=1_000_000), [])

    async def test_hard_qualification_is_separate_from_utility_ranking(self):
        fabric = McpFabric(store=self.store, authority=self.authority)
        unsafe = McpCandidate(
            server_id="unsafe",
            version="1",
            digest="sha256:" + "1" * 64,
            transport_kind="streamable-http",
            tools=("resource.inspect",),
            permissions=(),
            identity_ok=False,
            auth_ok=True,
            sandboxable=True,
            hard_denies=(),
            successes=10_000,
            failures=0,
        )
        qualification = fabric.qualify(unsafe)
        self.assertFalse(qualification.eligible)
        self.assertIn("identity", qualification.reasons)


if __name__ == "__main__":
    unittest.main()
