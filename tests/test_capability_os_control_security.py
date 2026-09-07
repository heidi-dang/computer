"""Control-plane regressions for caller-supplied trust claims."""
import unittest
from tests import test_capability_os_authority as authority_fixtures
from tests import test_capability_os_control_api as control_fixtures
from cptr.services.capability_os.authority import AuthorityDenied, LeaseRequest, TaskAuthorityPolicy
from cptr.services.capability_os.contracts import CapabilityRequest, ArtifactState


class ControlTrustSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = control_fixtures.CapabilityOsControlApiTests()
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)

    async def draft(self):
        result = await self.f.service.forge(
            user_id="user-1", task_id="task-1", operation="create",
            payload={"toolId":"tool.security", "version":"1", "runtimeClass":"gvisor",
                     "entrypoint":"main.py", "files":{"main.py":"pass\n"},
                     "requestedCapabilities":[{"action":"filesystem.read","resource":"repo:cptr/**"}]})
        return result["artifact"]["metadata"]["contentDigest"]

    async def test_arbitrary_evidence_id_cannot_promote_tool(self):
        digest = await self.draft()
        with self.assertRaises((PermissionError, ValueError)):
            await self.f.service.forge(user_id="user-1",task_id="task-1",operation="persist",
                payload={"contentDigest":digest,"targetState":ArtifactState.QUALIFIED.value,
                         "evidenceIds":["invented-proof"]})

    async def test_client_observation_cannot_impersonate_trusted_build_producer(self):
        digest = await self.draft()
        await self.f.service.reflect(user_id="user-1",task_id="task-1",kind="tool.build",
                                    claims={"exitCode":0},artifact_digest=digest)
        rows=await self.f.store.list_evidence("task-1")
        self.assertNotEqual(rows[0].producer_identity, "capability-os-control")

    async def test_inspect_hides_other_tasks_ephemeral_artifacts(self):
        digest = await self.draft()
        from dataclasses import replace
        self.f.service.tasks.context=replace(self.f.service.tasks.context,task_id="task-2")
        result=await self.f.service.inspect(user_id="user-1",task_id="task-2")
        self.assertNotIn(digest,[a["metadata"]["contentDigest"] for a in result["artifacts"]])


class ApprovalTrustSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_arbitrary_approval_id_does_not_authorize_critical_permission(self):
        f=authority_fixtures.CapabilityOsAuthorityTests()
        await f.asyncSetUp()
        self.addAsyncCleanup(f.asyncTearDown)
        permission=CapabilityRequest("production.deploy","service:cptr-backend")
        request=LeaseRequest(task_id="task-1",workload_id="w",
            artifact_digest=f.artifact.metadata.content_digest,permissions=(permission,),
            runtime_profile="gvisor",requested_lease_ms=1000)
        with self.assertRaises(AuthorityDenied):
            await f.broker.issue(request,policy=TaskAuthorityPolicy(allowed=(permission,)),
                                 approval_id="invented-approval")
