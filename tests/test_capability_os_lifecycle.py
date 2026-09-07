import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, WorkbenchSession
from cptr.services.capability_os.authority import AuthorityBroker, LeaseRequest, TaskAuthorityPolicy
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    CapabilityRequest,
    create_artifact,
)
from cptr.services.capability_os.lifecycle import (
    reconcile_inactive_task_authority,
    revoke_task_authority,
)
from cptr.services.capability_os.store import SqlCapabilityOsStore


class CapabilityOsLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=self.sessions)
        self.authority = AuthorityBroker(store=self.store, clock_ms=lambda: 1_000)
        self.permission = CapabilityRequest("mcp.invoke", "mcp:server/*")

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _workbench(self, session_id: str, status: str):
        async with self.sessions() as db:
            db.add(
                WorkbenchSession(
                    id=session_id,
                    user_id="user-1",
                    name=session_id,
                    status=status,
                    event_count=0,
                    created_at=1,
                    updated_at=1,
                )
            )
            await db.commit()

    async def _lease(self, task_id: str, *, lease_ms: int = 30_000, suffix: str = "1"):
        artifact = create_artifact(
            artifact_id=f"mcp.server-{suffix}",
            version="1",
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.MCP,
            spec={"serverId": f"server-{suffix}", "transport": "streamable-http"},
            created_at="2026-09-07T12:00:00Z",
            user_id="user-1",
            task_origin=task_id,
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(artifact)
        return await self.authority.issue(
            LeaseRequest(
                task_id=task_id,
                workload_id=f"mcp:server-{suffix}",
                artifact_digest=artifact.metadata.content_digest,
                permissions=(self.permission,),
                runtime_profile="remote-mcp",
                requested_lease_ms=lease_ms,
            ),
            policy=TaskAuthorityPolicy(
                allowed=(self.permission,),
                max_lease_ms=lease_ms,
            ),
        )

    async def _mount(self, task_id: str, lease, suffix: str):
        return await self.store.create_mcp_mount(
            task_id=task_id,
            server_id=f"server-{suffix}",
            version="1",
            digest=lease.artifact_digest,
            lease_id=lease.lease_id,
            projected_tools=("inspect",),
            transport_kind="streamable-http",
            now_ms=1_000,
        )

    async def test_revoke_task_authority_is_idempotent_and_preserves_history(self):
        await self._workbench("wb-terminal", "ARCHIVED")
        lease = await self._lease("wb-terminal")
        mount = await self._mount("wb-terminal", lease, "terminal")

        first = await revoke_task_authority(
            "wb-terminal", store=self.store, now_ms=2_000
        )
        second = await revoke_task_authority(
            "wb-terminal", store=self.store, now_ms=2_001
        )
        self.assertEqual(first, {"releasedMounts": 1, "revokedLeases": 1})
        self.assertEqual(second, {"releasedMounts": 0, "revokedLeases": 0})
        self.assertEqual((await self.store.get_lease(lease.lease_id)).status, "revoked")
        self.assertEqual((await self.store.get_mcp_mount(mount.mount_id)).state, "released")

    async def test_reconciler_repairs_waiting_orphan_and_expired_authority_only(self):
        await self._workbench("wb-active", "OPEN")
        await self._workbench("wb-review", "REVIEW_REQUIRED")

        active = await self._lease("wb-active", suffix="1")
        expired = await self._lease("wb-active", lease_ms=500, suffix="2")
        waiting = await self._lease("wb-review", suffix="3")
        orphan = await self._lease("missing-task", suffix="4")
        active_mount = await self._mount("wb-active", active, "active")
        expired_mount = await self._mount("wb-active", expired, "expired")
        waiting_mount = await self._mount("wb-review", waiting, "waiting")
        orphan_mount = await self._mount("missing-task", orphan, "orphan")

        report = await reconcile_inactive_task_authority(
            store=self.store,
            session_factory=self.sessions,
            now_ms=2_000,
        )
        self.assertEqual(report["inactiveTasks"], 2)
        self.assertEqual(report["inactiveTaskReasons"]["wb-review"], "workbench:REVIEW_REQUIRED")
        self.assertEqual(report["inactiveTaskReasons"]["missing-task"], "orphaned-task")
        self.assertGreaterEqual(report["revokedExpiredLeases"], 1)

        self.assertEqual((await self.store.get_lease(active.lease_id)).status, "active")
        self.assertEqual((await self.store.get_mcp_mount(active_mount.mount_id)).state, "mounted")
        for lease in (expired, waiting, orphan):
            self.assertEqual((await self.store.get_lease(lease.lease_id)).status, "revoked")
        for mount in (expired_mount, waiting_mount, orphan_mount):
            self.assertEqual((await self.store.get_mcp_mount(mount.mount_id)).state, "released")


if __name__ == "__main__":
    unittest.main()
