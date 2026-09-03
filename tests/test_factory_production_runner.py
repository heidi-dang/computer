import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.factory_capabilities import CapabilityInventory
from cptr.services.factory_domain import FactoryState
from cptr.services.factory_orchestrator import FactoryOrchestrator
from cptr.services.factory_phases import PhaseContext, RecoveryPhaseHandler
from cptr.services.factory_production import (
    FactoryProductionRunner,
    ProductionCiPhaseHandler,
    build_production_orchestrator,
)
from cptr.services.factory_runtime import FactoryRuntime
from cptr.services.factory_store import SqlFactoryStore


class FactoryProductionRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlFactoryStore(session_factory=self.sessions)

    async def asyncTearDown(self):
        await self.engine.dispose()

    @staticmethod
    def _git_repo() -> tempfile.TemporaryDirectory:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "factory@example.invalid"], check=True
        )
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Factory Test"], check=True)
        (root / "test_smoke.py").write_text(
            "def test_smoke():\n    assert 2 + 2 == 4\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(root), "add", "test_smoke.py"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
        return temp

    async def test_no_mutation_machine_verified_run_reaches_complete(self):
        temp = self._git_repo()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="prove the production runner can complete a verified no-mutation mission",
            acceptance_criteria=["the fixed local smoke test passes"],
            policy={
                "max_cycles": 1,
                "implementation_required": False,
                "push_required": False,
                "ci_required": False,
                "verification_targets": [
                    {
                        "gate_id": "smoke-tests",
                        "phase": "full",
                        "target": "python_pytest",
                        "test_path": "test_smoke.py",
                        "category": "broader_tests",
                        "acceptance_ids": [1],
                    }
                ],
            },
            budget={},
            model_id=None,
            idempotency_key="production-no-mutation",
        )
        builtin = CapabilityInventory._builtin_manifests()[0]
        orchestrator = build_production_orchestrator(
            store=self.store,
            owner_token="production-test",
            lease_ms=10_000,
        )
        workspace = SimpleNamespace(id="workspace-1", path=str(root))
        with (
            patch(
                "cptr.services.factory_production._workspace", new=AsyncMock(return_value=workspace)
            ),
            patch(
                "cptr.services.factory_production._repo_root", new=AsyncMock(return_value=str(root))
            ),
            patch(
                "cptr.services.factory_production.identity_for_user_id",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                CapabilityInventory, "discover_local", new=AsyncMock(return_value=[builtin])
            ),
        ):
            for _ in range(40):
                current = await self.store.get_run(run.id)
                if current.state == FactoryState.COMPLETE.value:
                    break
                await orchestrator.run_once(run.id)
            completed = await self.store.get_run(run.id)

        self.assertEqual(completed.state, FactoryState.COMPLETE.value)
        cycle = await self.store.get_cycle(completed.current_cycle_id)
        self.assertIsNotNone(cycle.target_revision)
        self.assertIsNotNone(cycle.target_fingerprint)
        gates = await self.store.list_gates(run.id, cycle_id=cycle.id)
        latest = {gate.gate_id: gate for gate in gates}
        self.assertEqual(latest["smoke-tests"].status, "PASS")
        self.assertEqual(latest["git-diff-check"].status, "PASS")
        events = await self.store.list_events(run.id, limit=200)
        self.assertTrue(any(event.event_type == "victory.authorized" for event in events))

    async def test_missing_machine_acceptance_coverage_blocks_at_baseline(self):
        temp = self._git_repo()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="must fail closed without acceptance proof",
            acceptance_criteria=["criterion requires evidence"],
            policy={"implementation_required": False, "verification_targets": []},
            budget={},
            model_id=None,
            idempotency_key="production-missing-gates",
        )
        orchestrator = build_production_orchestrator(
            store=self.store,
            owner_token="production-test",
            lease_ms=10_000,
        )
        workspace = SimpleNamespace(id="workspace-1", path=str(root))
        with (
            patch(
                "cptr.services.factory_production._workspace", new=AsyncMock(return_value=workspace)
            ),
            patch(
                "cptr.services.factory_production._repo_root", new=AsyncMock(return_value=str(root))
            ),
            patch(
                "cptr.services.factory_production.identity_for_user_id",
                new=AsyncMock(return_value=None),
            ),
        ):
            await orchestrator.run_once(run.id)  # mission -> recovering
            await orchestrator.run_once(run.id)  # recovering -> baselining
            blocked = await orchestrator.run_once(run.id)

        self.assertEqual(blocked.state, FactoryState.BLOCKED.value)
        self.assertIn("machine verification", blocked.next_action or "")

    async def test_restart_after_run_create_can_create_initial_cycle_from_recovering(self):
        run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="recover immediately after durable run creation",
            acceptance_criteria=["recovery creates exactly one initial cycle"],
            policy={},
            budget={},
            model_id=None,
            idempotency_key="production-restart-before-cycle",
        )
        runtime = FactoryRuntime(
            store=self.store,
            owner_token="runtime-test",
            lease_ms=10_000,
        )
        recovered = await runtime.reconcile_run(run.id, idempotency_key="recover-before-cycle")
        self.assertEqual(recovered.state, FactoryState.RECOVERING.value)
        self.assertIsNone(recovered.current_cycle_id)
        orchestrator = FactoryOrchestrator(
            store=self.store,
            handlers={FactoryState.RECOVERING: RecoveryPhaseHandler()},
            owner_token="orchestrator-test",
            lease_ms=10_000,
        )

        resumed = await orchestrator.run_once(run.id)

        self.assertEqual(resumed.state, FactoryState.BASELINING.value)
        self.assertIsNotNone(resumed.current_cycle_id)
        cycles = await self.store.list_cycles(run.id)
        self.assertEqual(len(cycles), 1)

    async def test_ci_pending_observation_is_replay_safe_until_terminal(self):
        revision = "a" * 40
        run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="observe exact revision CI without phase replay conflicts",
            acceptance_criteria=["required workflow passes"],
            policy={
                "ci_required": True,
                "push_required": True,
                "ci_repository": "example/repo",
                "ci_workflows": ["Tests"],
            },
            budget={},
            model_id=None,
            idempotency_key="production-ci-replay-safe",
        )
        cycle = await self.store.create_cycle(
            run.id,
            base_revision="base",
            base_fingerprint="base-fingerprint",
            idempotency_key="production-ci-cycle",
        )
        await self.store.set_cycle_target(
            run.id,
            cycle.id,
            revision=revision,
            fingerprint="target-fingerprint",
            idempotency_key="production-ci-target",
        )
        run = await self.store.get_run(run.id)
        cycle = await self.store.get_cycle(cycle.id)

        class _Git:
            async def get_intent_for_cycle(self, _cycle_id):
                return SimpleNamespace(status="COMMITTED", commit_sha=revision)

        class _Provider:
            async def discover(self, *, repository, revision):
                return (
                    SimpleNamespace(
                        external_run_id="101",
                        workflow="Tests",
                        url="https://github.com/example/repo/actions/runs/101",
                    ),
                )

        class _Ci:
            def __init__(self):
                self.polls = 0

            async def begin_tracking(self, **_kwargs):
                return SimpleNamespace(id="ci-1")

            async def poll_once(self, _ci_run_id):
                self.polls += 1
                completed = self.polls > 1
                return SimpleNamespace(
                    revision=revision,
                    repository="example/repo",
                    check_id="Tests",
                    external_run_id="101",
                    status="COMPLETED" if completed else "IN_PROGRESS",
                    conclusion="SUCCESS" if completed else None,
                    url="https://github.com/example/repo/actions/runs/101",
                )

        ci = _Ci()
        handler = ProductionCiPhaseHandler(git_service=_Git(), ci_service=ci, provider=_Provider())
        context = PhaseContext(run=run, cycle=cycle, evidence=(), gates=())

        pending = await handler.execute(context)
        completed = await handler.execute(context)

        self.assertIsNone(pending.next_state)
        self.assertEqual(pending.artifacts, ())
        self.assertIsNone(pending.run_next_action)
        self.assertEqual(completed.next_state, FactoryState.CYCLE_COMPLETE)
        self.assertEqual(len(completed.artifacts), 1)
        self.assertEqual(completed.artifacts[0].payload["status"], "COMPLETED")
        self.assertEqual(completed.artifacts[0].payload["conclusion"], "SUCCESS")

    async def test_scheduler_is_single_flight_and_stops_at_waiting_state(self):
        run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="wait safely",
            acceptance_criteria=["pause is durable"],
            policy={},
            budget={},
            model_id=None,
            idempotency_key="production-scheduler-single-flight",
        )
        observed = SimpleNamespace(id=run.id, state=FactoryState.PAUSED.value)
        orchestrator = SimpleNamespace(run_once=AsyncMock(return_value=observed))
        runner = FactoryProductionRunner(
            store=self.store,
            lease_ms=10_000,
            poll_interval=0.001,
            orchestrator=orchestrator,
        )
        runner.schedule(run.id)
        runner.schedule(run.id)
        await asyncio.sleep(0.01)
        await runner.close()

        orchestrator.run_once.assert_awaited_once_with(run.id)


if __name__ == "__main__":
    unittest.main()
