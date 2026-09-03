import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.factory_ci import CiObservation, FactoryCiService
from cptr.services.factory_domain import FactoryState
from cptr.services.factory_git import FactoryGitService, PushAuthorization
from cptr.services.factory_phases import PhaseContext, PhaseFailureCategory
from cptr.services.factory_phases.lifecycle import (
    CiTrackingIdentity,
    CiVerifyingPhaseHandler,
    CommittingPhaseHandler,
    PushingPhaseHandler,
)
from cptr.services.factory_store import SqlFactoryStore
from test_factory_git_ci import _CiProvider, _FakeGit


class FactoryLifecyclePhaseHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlFactoryStore(session_factory=self.sessions)
        self.run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="complete Git and CI lifecycle",
            acceptance_criteria=["verified change reaches successful CI"],
            policy={},
            budget={},
            model_id="configured-model",
            idempotency_key="phase7-lifecycle-handler",
        )
        self.cycle = await self.store.create_cycle(
            self.run.id,
            base_revision="rev-base",
            base_fingerprint="fp-base",
            idempotency_key="cycle-1",
        )
        await self.store.set_cycle_target(
            self.run.id,
            self.cycle.id,
            revision="rev-verified",
            fingerprint="fp-verified",
            idempotency_key="target-verified",
        )
        self.git = _FakeGit()
        self.git_service = FactoryGitService(session_factory=self.sessions, git=self.git)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _context(self):
        return PhaseContext(
            run=await self.store.get_run(self.run.id),
            cycle=await self.store.get_cycle(self.cycle.id),
            evidence=(),
            gates=(),
        )

    async def test_committing_handler_creates_restart_safe_intent_and_machine_artifact(self):
        async def repo_root(_context):
            return "/repo"

        handler = CommittingPhaseHandler(
            git_service=self.git_service,
            repo_root_resolver=repo_root,
            repository_key=".",
            commit_message="factory: verified lifecycle",
        )

        outcome = await handler.execute(await self._context())

        self.assertEqual(outcome.next_state, FactoryState.PUSHING)
        self.assertEqual(len(outcome.artifacts), 1)
        artifact = outcome.artifacts[0]
        self.assertEqual(artifact.kind, "git_commit")
        self.assertEqual(artifact.revision, "commit-sha-1")
        self.assertEqual(artifact.payload["changed_paths"], ["src/app.py"])
        self.assertEqual(len(self.git.commit_calls), 1)

    async def test_pushing_handler_requests_approval_before_git_provider_invocation(self):
        intent = await self.git_service.prepare_commit_intent(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            repo_root="/repo",
            repository_key=".",
            message="factory: approval lifecycle",
        )
        intent = await self.git_service.commit_intent(intent.id, repo_root="/repo")

        async def repo_root(_context):
            return "/repo"

        async def no_approval(_context, _intent):
            return None

        pending = await PushingPhaseHandler(
            git_service=self.git_service,
            repo_root_resolver=repo_root,
            authorization_resolver=no_approval,
        ).execute(await self._context())

        self.assertEqual(pending.next_state, FactoryState.APPROVAL_REQUIRED)
        self.assertEqual(self.git.push_calls, [])

        async def approved(_context, current):
            return PushAuthorization(
                approved=True,
                approval_id="approval-1",
                revision=current.commit_sha,
                remote="origin",
                branch="feature",
            )

        pushed = await PushingPhaseHandler(
            git_service=self.git_service,
            repo_root_resolver=repo_root,
            authorization_resolver=approved,
        ).execute(await self._context())

        self.assertEqual(pushed.next_state, FactoryState.CI_VERIFYING)
        self.assertEqual(pushed.artifacts[0].kind, "git_push")
        self.assertEqual(self.git.push_calls, [("origin", "feature")])

    async def test_ci_handler_observes_once_per_call_and_transitions_only_on_success(self):
        provider = _CiProvider(
            [
                CiObservation(status="IN_PROGRESS"),
                CiObservation(status="COMPLETED", conclusion="SUCCESS"),
            ]
        )
        service = FactoryCiService(session_factory=self.sessions, providers={"github": provider})

        async def identity(_context):
            return CiTrackingIdentity(
                provider="github",
                repository="heidi-dang/computer",
                revision="commit-sha-1",
                external_run_id="run-1",
                check_id="check-1",
            )

        handler = CiVerifyingPhaseHandler(ci_service=service, identity_resolver=identity)

        pending = await handler.execute(await self._context())
        self.assertIsNone(pending.next_state)
        self.assertEqual(pending.artifacts, ())
        self.assertEqual(len(provider.calls), 1)

        passed = await handler.execute(await self._context())
        self.assertEqual(passed.next_state, FactoryState.CYCLE_COMPLETE)
        self.assertEqual(passed.artifacts[0].kind, "ci_result")
        self.assertEqual(passed.artifacts[0].payload["conclusion"], "SUCCESS")
        self.assertEqual(len(provider.calls), 2)

    async def test_ci_handler_classifies_terminal_failure_for_repair_loop(self):
        provider = _CiProvider(
            [
                CiObservation(
                    status="COMPLETED",
                    conclusion="FAILURE",
                    failure_summary="unit:test_widget failed",
                )
            ]
        )
        service = FactoryCiService(session_factory=self.sessions, providers={"github": provider})

        async def identity(_context):
            return CiTrackingIdentity(
                provider="github",
                repository="heidi-dang/computer",
                revision="commit-sha-1",
                external_run_id="run-failure",
            )

        outcome = await CiVerifyingPhaseHandler(
            ci_service=service,
            identity_resolver=identity,
        ).execute(await self._context())

        self.assertIsNone(outcome.next_state)
        self.assertIsNotNone(outcome.failure)
        self.assertEqual(outcome.failure.category, PhaseFailureCategory.CI)
        self.assertEqual(outcome.failure.code, "CI_FAILURE")
        self.assertEqual(outcome.artifacts[0].kind, "ci_result")


if __name__ == "__main__":
    unittest.main()
