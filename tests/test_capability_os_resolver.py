import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    CapabilityRequest,
    create_artifact,
)
from cptr.services.capability_os.resolver import CapabilityResolver, ResolutionGoal
from cptr.services.capability_os.store import SqlCapabilityOsStore


class CapabilityOsResolverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=sessions)
        self.resolver = CapabilityResolver(store=self.store)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _tool(self, *, tool_id, state, capabilities, user_id=None):
        artifact = create_artifact(
            artifact_id=tool_id,
            version="1",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec={"requestedCapabilities": [item.to_dict() for item in capabilities]},
            created_at="2026-09-07T01:00:00Z",
            user_id=user_id,
            task_origin="task-1",
            state=state,
        )
        await self.store.persist_artifact(artifact)
        return artifact

    async def test_resolver_prefers_reusable_qualified_exact_fit_and_excludes_forbidden_effect(self):
        good = await self._tool(
            tool_id="logs.read",
            state=ArtifactState.QUALIFIED,
            capabilities=(CapabilityRequest("logs.read", "service:cptr/**"),),
        )
        await self._tool(
            tool_id="dangerous",
            state=ArtifactState.CERTIFIED,
            capabilities=(
                CapabilityRequest("logs.read", "service:cptr/**"),
                CapabilityRequest("secrets.read", "service:cptr/**"),
            ),
        )
        result = await self.resolver.resolve(
            ResolutionGoal(
                task_id="task-1",
                required=(CapabilityRequest("logs.read", "service:cptr/backend"),),
                optional=(),
                forbidden=(CapabilityRequest("secrets.read", "service:cptr/**"),),
            )
        )
        self.assertEqual(result.candidates[0].content_digest, good.metadata.content_digest)
        self.assertEqual(result.missing, ())
        self.assertEqual(result.acquisition_modes, ())
        self.assertTrue(all(item.artifact_id != "dangerous" for item in result.candidates))

    async def test_ephemeral_artifact_is_reusable_only_by_originating_task(self):
        ephemeral = await self._tool(
            tool_id="task-probe",
            state=ArtifactState.EPHEMERAL,
            capabilities=(CapabilityRequest("trace.read", "service:cptr/**"),),
        )
        same = await self.resolver.resolve(
            ResolutionGoal(
                task_id="task-1",
                required=(CapabilityRequest("trace.read", "service:cptr/backend"),),
                optional=(),
                forbidden=(),
            )
        )
        self.assertEqual(same.candidates[0].content_digest, ephemeral.metadata.content_digest)

        other = await self.resolver.resolve(
            ResolutionGoal(
                task_id="task-2",
                required=(CapabilityRequest("trace.read", "service:cptr/backend"),),
                optional=(),
                forbidden=(),
            )
        )
        self.assertEqual(other.candidates, ())
        self.assertEqual(other.acquisition_modes, ("forge", "mcp"))

    async def test_user_scoped_artifact_is_invisible_to_other_users_but_global_remains_visible(self):
        private = await self._tool(
            tool_id="private.logs",
            state=ArtifactState.QUALIFIED,
            capabilities=(CapabilityRequest("logs.read", "service:cptr/**"),),
            user_id="user-a",
        )
        global_tool = await self._tool(
            tool_id="global.metrics",
            state=ArtifactState.CORE,
            capabilities=(CapabilityRequest("metrics.read", "service:cptr/**"),),
        )
        other = await self.resolver.resolve(
            ResolutionGoal(
                task_id="task-1",
                required=(CapabilityRequest("logs.read", "service:cptr/backend"),),
                optional=(),
                forbidden=(),
            ),
            user_id="user-b",
        )
        self.assertEqual(other.candidates, ())
        owner = await self.resolver.resolve(
            ResolutionGoal(
                task_id="task-1",
                required=(CapabilityRequest("logs.read", "service:cptr/backend"),),
                optional=(),
                forbidden=(),
            ),
            user_id="user-a",
        )
        self.assertEqual(owner.candidates[0].content_digest, private.metadata.content_digest)
        global_result = await self.resolver.resolve(
            ResolutionGoal(
                task_id="task-1",
                required=(CapabilityRequest("metrics.read", "service:cptr/backend"),),
                optional=(),
                forbidden=(),
            ),
            user_id="user-b",
        )
        self.assertEqual(global_result.candidates[0].content_digest, global_tool.metadata.content_digest)

    async def test_missing_ability_returns_competing_acquisition_modes_not_automatic_execution(self):
        result = await self.resolver.resolve(
            ResolutionGoal(
                task_id="task-1",
                required=(CapabilityRequest("kubernetes.pods.read", "cluster:staging"),),
                optional=(CapabilityRequest("kubernetes.logs.read", "cluster:staging"),),
                forbidden=(CapabilityRequest("kubernetes.secrets.read", "cluster:staging"),),
            )
        )
        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.missing), 1)
        self.assertEqual(result.acquisition_modes, ("forge", "mcp"))


if __name__ == "__main__":
    unittest.main()
