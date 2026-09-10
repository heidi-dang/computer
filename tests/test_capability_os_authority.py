import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.capability_os.authority import (
    AuthorityBroker,
    AuthorityDenied,
    LeaseRequest,
    TaskAuthorityPolicy,
)
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    CapabilityRequest,
    create_artifact,
)
from cptr.services.capability_os.store import SqlCapabilityOsStore


class CapabilityOsAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=self.sessions)
        async def verify_approval(approval_id, request, critical_permissions):
            return (
                approval_id == "approval-1"
                and request.task_id == "task-1"
                and request.workload_id == "workload-1"
                and all(permission.action == "production.deploy" for permission in critical_permissions)
            )
        self.broker = AuthorityBroker(
            store=self.store,
            clock_ms=lambda: 1_000_000,
            approval_verifier=verify_approval,
        )
        self.artifact = create_artifact(
            artifact_id="tool.readonly",
            version="1",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec={"runtime": {"class": "gvisor"}},
            created_at="2026-09-07T01:00:00Z",
            task_origin="task-1",
        )
        await self.store.persist_artifact(self.artifact)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_generated_artifact_receives_only_task_policy_subset_and_ttl(self):
        policy = TaskAuthorityPolicy(
            allowed=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            forbidden=(CapabilityRequest("filesystem.write", "repo:cptr/prod/**"),),
            max_lease_ms=30_000,
        )
        lease = await self.broker.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="workload-1",
                artifact_digest=self.artifact.metadata.content_digest,
                permissions=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
                runtime_profile="gvisor",
                requested_lease_ms=120_000,
            ),
            policy=policy,
        )
        self.assertEqual(lease.status, "active")
        self.assertEqual(lease.expires_at_ms, 1_030_000)
        self.assertEqual(lease.permissions[0].action, "filesystem.read")

    async def test_permission_expansion_and_unknown_artifact_fail_closed(self):
        policy = TaskAuthorityPolicy(
            allowed=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            max_lease_ms=30_000,
        )
        with self.assertRaises(AuthorityDenied):
            await self.broker.issue(
                LeaseRequest(
                    task_id="task-1",
                    workload_id="workload-1",
                    artifact_digest=self.artifact.metadata.content_digest,
                    permissions=(CapabilityRequest("filesystem.write", "repo:cptr/**"),),
                    runtime_profile="gvisor",
                    requested_lease_ms=10_000,
                ),
                policy=policy,
            )
        with self.assertRaises(AuthorityDenied):
            await self.broker.issue(
                LeaseRequest(
                    task_id="task-1",
                    workload_id="workload-1",
                    artifact_digest="sha256:" + "0" * 64,
                    permissions=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
                    runtime_profile="gvisor",
                    requested_lease_ms=10_000,
                ),
                policy=policy,
            )
        self.assertEqual(await self.store.list_active_leases("task-1", now_ms=1_000_000), [])

    async def test_authority_critical_permission_requires_explicit_approval(self):
        policy = TaskAuthorityPolicy(
            allowed=(CapabilityRequest("production.deploy", "service:cptr-backend"),),
            max_lease_ms=30_000,
        )
        request = LeaseRequest(
            task_id="task-1",
            workload_id="workload-1",
            artifact_digest=self.artifact.metadata.content_digest,
            permissions=(CapabilityRequest("production.deploy", "service:cptr-backend"),),
            runtime_profile="gvisor",
            requested_lease_ms=10_000,
        )
        with self.assertRaises(AuthorityDenied):
            await self.broker.issue(request, policy=policy)
        lease = await self.broker.issue(request, policy=policy, approval_id="approval-1")
        self.assertEqual(lease.approval_id, "approval-1")

    async def test_relaxed_critical_approval_still_requires_standing_policy(self):
        critical = CapabilityRequest("production.deploy", "service:cptr-backend")
        request = LeaseRequest(
            task_id="task-1",
            workload_id="workload-1",
            artifact_digest=self.artifact.metadata.content_digest,
            permissions=(critical,),
            runtime_profile="gvisor",
            requested_lease_ms=10_000,
        )
        allowed_policy = TaskAuthorityPolicy(allowed=(critical,), max_lease_ms=30_000)
        lease = await self.broker.issue(
            request,
            policy=allowed_policy,
            require_critical_approval=False,
        )
        self.assertIsNone(lease.approval_id)

        denied_policy = TaskAuthorityPolicy(
            allowed=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            max_lease_ms=30_000,
        )
        with self.assertRaises(AuthorityDenied):
            await self.broker.issue(
                request,
                policy=denied_policy,
                require_critical_approval=False,
            )

    async def test_request_resource_limits_cannot_widen_server_policy(self):
        policy = TaskAuthorityPolicy(
            allowed=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            max_lease_ms=30_000,
            max_calls=10,
            resource_limits={"memoryMiB": 256, "cpuMillis": 1000},
        )
        base = dict(
            task_id="task-1", workload_id="workload-resource",
            artifact_digest=self.artifact.metadata.content_digest,
            permissions=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            runtime_profile="gvisor", requested_lease_ms=10_000,
        )
        with self.assertRaises(AuthorityDenied):
            await self.broker.issue(LeaseRequest(**base, resource_limits={"memoryMiB": 512}), policy=policy)
        with self.assertRaises(AuthorityDenied):
            await self.broker.issue(LeaseRequest(**base, resource_limits={"maxCalls": 999}), policy=policy)
        lease = await self.broker.issue(
            LeaseRequest(**base, resource_limits={"memoryMiB": 128, "maxCalls": 5}), policy=policy
        )
        self.assertEqual(lease.resource_limits["memoryMiB"], 128)
        self.assertEqual(lease.resource_limits["cpuMillis"], 1000)
        self.assertEqual(lease.resource_limits["maxCalls"], 5)

    async def test_existing_lease_cannot_be_reused_outside_permission_runtime_or_resource_scope(self):
        policy = TaskAuthorityPolicy(
            allowed=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            max_lease_ms=30_000, resource_limits={"memoryMiB": 256},
        )
        lease = await self.broker.issue(
            LeaseRequest(
                task_id="task-1", workload_id="scoped", artifact_digest=self.artifact.metadata.content_digest,
                permissions=(CapabilityRequest("filesystem.read", "repo:cptr/**"),), runtime_profile="gvisor",
                requested_lease_ms=10_000, resource_limits={"memoryMiB": 128},
            ), policy=policy
        )
        with self.assertRaises(AuthorityDenied):
            await self.broker.require_active(
                lease.lease_id, task_id="task-1", artifact_digest=self.artifact.metadata.content_digest,
                required_permissions=(CapabilityRequest("filesystem.write", "repo:cptr/**"),),
            )
        with self.assertRaises(AuthorityDenied):
            await self.broker.require_active(
                lease.lease_id, task_id="task-1", artifact_digest=self.artifact.metadata.content_digest,
                runtime_profile="remote-mcp",
            )
        with self.assertRaises(AuthorityDenied):
            await self.broker.require_active(
                lease.lease_id, task_id="task-1", artifact_digest=self.artifact.metadata.content_digest,
                resource_limits={"memoryMiB": 200},
            )
        checked = await self.broker.require_active(
            lease.lease_id, task_id="task-1", artifact_digest=self.artifact.metadata.content_digest,
            required_permissions=(CapabilityRequest("filesystem.read", "repo:cptr/file"),),
            runtime_profile="gvisor", resource_limits={"memoryMiB": 64},
        )
        self.assertEqual(checked.lease_id, lease.lease_id)

    async def test_task_close_revokes_every_active_lease_and_asserts_zero_authority(self):
        policy = TaskAuthorityPolicy(
            allowed=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
            max_lease_ms=30_000,
        )
        await self.broker.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="workload-a",
                artifact_digest=self.artifact.metadata.content_digest,
                permissions=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
                runtime_profile="gvisor",
                requested_lease_ms=10_000,
            ),
            policy=policy,
        )
        await self.broker.issue(
            LeaseRequest(
                task_id="task-1",
                workload_id="workload-b",
                artifact_digest=self.artifact.metadata.content_digest,
                permissions=(CapabilityRequest("filesystem.read", "repo:cptr/**"),),
                runtime_profile="gvisor",
                requested_lease_ms=10_000,
            ),
            policy=policy,
        )
        revoked = await self.broker.close_task("task-1")
        self.assertEqual(revoked, 2)
        self.assertEqual(await self.store.list_active_leases("task-1", now_ms=1_000_000), [])


if __name__ == "__main__":
    unittest.main()
