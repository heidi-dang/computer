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
from cptr.services.factory_domain import FactoryActor, FactoryState
from cptr.services.factory_orchestrator import FactoryOrchestrator
from cptr.services.factory_phases import PhaseContext, RecoveryPhaseHandler
from cptr.services.factory_production import (
    AdvisoryPhaseHandler,
    BaselinePhaseHandler,
    FactoryProductionRunner,
    ImplementationPhaseHandler,
    ProductionCiPhaseHandler,
    _reset_isolated_reproduction,
    _run_fixed_target,
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

    async def test_python_verification_uses_current_interpreter(self):
        process = SimpleNamespace(
            returncode=0,
            communicate=AsyncMock(return_value=(b"ok", b"")),
            kill=lambda: None,
        )
        spec = SimpleNamespace(
            path=".",
            test_path="test_smoke.py",
            target="python_pytest",
            timeout_seconds=5.0,
        )
        with tempfile.TemporaryDirectory() as root:
            with (
                patch("cptr.services.factory_production.sys.executable", "/opt/cptr/python"),
                patch(
                    "cptr.services.factory_production.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=process),
                ) as spawn,
            ):
                result = await _run_fixed_target(root, spec)

        self.assertTrue(result["passed"])
        self.assertEqual(
            spawn.await_args.args[:4],
            ("/opt/cptr/python", "-m", "pytest", "test_smoke.py"),
        )

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

    async def test_advisory_recovers_redacted_legacy_phase_evidence(self):
        agent = SimpleNamespace(
            start_task=AsyncMock(),
            get_task=AsyncMock(
                return_value={
                    "id": "task-1",
                    "status": "COMPLETE",
                    "created_at": 1,
                    "raw_output": [],
                    "output": "bounded understanding",
                }
            ),
            get_output=AsyncMock(return_value={"content": "bounded understanding"}),
        )
        handler = AdvisoryPhaseHandler(
            state=FactoryState.UNDERSTANDING,
            next_state=FactoryState.AUDITING,
            agent=agent,
        )
        context = SimpleNamespace(
            run=SimpleNamespace(
                id="run-1",
                user_id="user-1",
                workspace_id="workspace-1",
                mission="understand quickly",
                acceptance_criteria=("find one actionable defect",),
                model_id="agent:model",
                policy={},
            ),
            cycle=SimpleNamespace(id="cycle-1", attempt_count=0),
            evidence=(
                SimpleNamespace(
                    kind="factory_phase_task",
                    payload={"state": "[REDACTED]", "attempt": 0, "task_id": "task-1"},
                    idempotency_key=(
                        "phase:run-1:cycle-1:UNDERSTANDING:entry-fev-1:artifact:phase-task"
                    ),
                ),
            ),
            gates=(),
        )

        outcome = await handler.execute(context)

        self.assertEqual(outcome.next_state, FactoryState.AUDITING)
        self.assertEqual(outcome.artifacts[0].payload["phase_state"], "UNDERSTANDING")
        self.assertNotIn("state", outcome.artifacts[0].payload)
        agent.start_task.assert_not_awaited()
        agent.get_task.assert_awaited_once_with("task-1", user_id="user-1")
        agent.get_output.assert_awaited_once_with("task-1", user_id="user-1")

    async def test_advisory_start_uses_fast_execution_contract_and_safe_phase_key(self):
        agent = SimpleNamespace(start_task=AsyncMock(return_value={"id": "task-fast"}))
        handler = AdvisoryPhaseHandler(
            state=FactoryState.AUDITING,
            next_state=FactoryState.SELECTING_FINDING,
            agent=agent,
        )
        context = SimpleNamespace(
            run=SimpleNamespace(
                id="run-fast",
                user_id="user-1",
                workspace_id="workspace-1",
                mission="audit quickly",
                acceptance_criteria=("find one actionable defect",),
                model_id="agent:model",
                policy={"advisory_timeout_seconds": 90, "advisory_max_tool_calls": 6},
            ),
            cycle=SimpleNamespace(id="cycle-fast", attempt_count=0),
            evidence=(),
            gates=(),
        )

        outcome = await handler.execute(context)

        prompt = agent.start_task.await_args.kwargs["prompt"]
        self.assertIn("finish within 90 seconds", prompt)
        self.assertIn("at most 6 tool actions", prompt)
        self.assertIn("do not run tests", prompt)
        self.assertIn("Stop exploring", prompt)
        payload = outcome.artifacts[0].payload
        self.assertEqual(payload["phase_state"], "AUDITING")
        self.assertEqual(payload["timeout_seconds"], 90)
        self.assertEqual(payload["max_tool_calls"], 6)
        self.assertEqual(payload["execution_scope"], "source_read_only")
        self.assertFalse(agent.start_task.await_args.kwargs["execution_policy"]["allow_commands"])
        self.assertNotIn("state", payload)

    async def test_advisory_handoff_reuses_meaningful_prior_evidence_and_tightens_budget(self):
        agent = SimpleNamespace(start_task=AsyncMock(return_value={"id": "task-handoff"}))
        handler = AdvisoryPhaseHandler(
            state=FactoryState.ROOT_CAUSE_ANALYSIS,
            next_state=FactoryState.PLANNING,
            agent=agent,
        )
        context = SimpleNamespace(
            run=SimpleNamespace(
                id="run-handoff",
                user_id="user-1",
                workspace_id="workspace-1",
                mission="diagnose quickly",
                acceptance_criteria=("identify root cause",),
                model_id="agent:model",
                policy={"advisory_timeout_seconds": 75, "advisory_max_tool_calls": 8},
            ),
            cycle=SimpleNamespace(id="cycle-handoff", attempt_count=0),
            evidence=(
                SimpleNamespace(
                    kind="reasoning_advice",
                    payload={
                        "phase_state": "AUDITING",
                        "summary": (
                            "Fast advisory budget reached before a final model summary; "
                            "continue with bounded observations."
                        ),
                    },
                ),
                SimpleNamespace(
                    kind="reasoning_advice",
                    payload={
                        "phase_state": "REPRODUCING",
                        "summary": "health leak reproduced in cptr/app.py; remove pid and uptime fields",
                    },
                ),
            ),
            gates=(),
        )

        outcome = await handler.execute(context)

        prompt = agent.start_task.await_args.kwargs["prompt"]
        self.assertIn("PRIOR PHASE EVIDENCE", prompt)
        self.assertIn("[REPRODUCING] health leak reproduced in cptr/app.py", prompt)
        self.assertNotIn("continue with bounded observations", prompt)
        self.assertIn("finish within 45 seconds", prompt)
        self.assertIn("at most 4 tool actions", prompt)
        payload = outcome.artifacts[0].payload
        self.assertEqual(payload["timeout_seconds"], 45)
        self.assertEqual(payload["max_tool_calls"], 4)
        self.assertGreater(payload["handoff_evidence_chars"], 0)

    async def test_reproduction_runs_commands_only_in_prepared_isolated_worker(self):
        agent = SimpleNamespace(start_task=AsyncMock(return_value={"id": "task-repro"}))
        handler = AdvisoryPhaseHandler(
            state=FactoryState.REPRODUCING,
            next_state=FactoryState.ROOT_CAUSE_ANALYSIS,
            agent=agent,
        )
        context = SimpleNamespace(
            run=SimpleNamespace(
                id="run-repro",
                user_id="user-1",
                workspace_id="workspace-source",
                mission="reproduce quickly",
                acceptance_criteria=("reproduce one defect",),
                model_id="agent:model",
                policy={"reproduction_timeout_seconds": 90, "reproduction_max_tool_calls": 8},
            ),
            cycle=SimpleNamespace(
                id="cycle-repro",
                attempt_count=0,
                mutation_worker_id="worker-1",
            ),
            evidence=(),
            gates=(),
        )

        with patch(
            "cptr.services.factory_production._factory_worker_workspace",
            new=AsyncMock(return_value=(Path("/isolated"), SimpleNamespace(id="workspace-worker"))),
        ):
            outcome = await handler.execute(context)

        self.assertIsNone(outcome.next_state)
        self.assertEqual(agent.start_task.await_args.kwargs["workspace_id"], "workspace-worker")
        self.assertTrue(agent.start_task.await_args.kwargs["execution_policy"]["allow_commands"])
        self.assertFalse(
            agent.start_task.await_args.kwargs["execution_policy"]["allow_file_writes"]
        )
        self.assertEqual(
            outcome.artifacts[0].payload["execution_scope"], "isolated_mutation_worker"
        )

    async def test_reproduction_handoff_tightens_budget_and_keeps_isolated_commands(self):
        agent = SimpleNamespace(start_task=AsyncMock(return_value={"id": "task-repro-handoff"}))
        handler = AdvisoryPhaseHandler(
            state=FactoryState.REPRODUCING,
            next_state=FactoryState.ROOT_CAUSE_ANALYSIS,
            agent=agent,
        )
        context = SimpleNamespace(
            run=SimpleNamespace(
                id="run-repro-handoff",
                user_id="user-1",
                workspace_id="workspace-source",
                mission="reproduce quickly from prior evidence",
                acceptance_criteria=("reproduce one defect",),
                model_id="agent:model",
                policy={"reproduction_timeout_seconds": 90, "reproduction_max_tool_calls": 10},
            ),
            cycle=SimpleNamespace(
                id="cycle-repro-handoff",
                attempt_count=0,
                mutation_worker_id="worker-1",
            ),
            evidence=(
                SimpleNamespace(
                    kind="reasoning_advice",
                    payload={
                        "phase_state": "AUDITING",
                        "summary": "targeted health test gap is already localized",
                    },
                ),
            ),
            gates=(),
        )

        with patch(
            "cptr.services.factory_production._factory_worker_workspace",
            new=AsyncMock(return_value=(Path("/isolated"), SimpleNamespace(id="workspace-worker"))),
        ):
            outcome = await handler.execute(context)

        prompt = agent.start_task.await_args.kwargs["prompt"]
        self.assertIn("PRIOR PHASE EVIDENCE", prompt)
        self.assertIn("finish within 45 seconds", prompt)
        self.assertIn("at most 6 tool actions", prompt)
        self.assertTrue(agent.start_task.await_args.kwargs["execution_policy"]["allow_commands"])
        payload = outcome.artifacts[0].payload
        self.assertEqual(payload["timeout_seconds"], 45)
        self.assertEqual(payload["max_tool_calls"], 6)
        self.assertGreater(payload["handoff_evidence_chars"], 0)
        self.assertEqual(payload["execution_scope"], "isolated_mutation_worker")

    async def test_advisory_cancels_over_tool_budget_and_advances_with_partial_advice(self):
        running = {
            "id": "task-budget",
            "status": "RUNNING",
            "created_at": 999_000,
            "output": "partial machine-guided finding",
            "raw_output": [
                {"type": "function_call", "call_id": "call-1"},
                {"type": "function_call", "call_id": "call-2"},
                {"type": "function_call", "call_id": "call-3"},
            ],
        }
        cancelled = {**running, "status": "CANCELLED"}
        agent = SimpleNamespace(
            get_task=AsyncMock(return_value=running),
            cancel_task=AsyncMock(return_value=cancelled),
            get_output=AsyncMock(),
        )
        handler = AdvisoryPhaseHandler(
            state=FactoryState.ROOT_CAUSE_ANALYSIS,
            next_state=FactoryState.PLANNING,
            agent=agent,
        )
        context = SimpleNamespace(
            run=SimpleNamespace(
                id="run-budget",
                user_id="user-1",
                workspace_id="workspace-1",
                mission="root cause quickly",
                acceptance_criteria=("identify root cause",),
                model_id="agent:model",
                policy={"advisory_timeout_seconds": 120, "advisory_max_tool_calls": 2},
            ),
            cycle=SimpleNamespace(id="cycle-budget", attempt_count=0),
            evidence=(
                SimpleNamespace(
                    kind="factory_phase_task",
                    payload={
                        "phase_state": "ROOT_CAUSE_ANALYSIS",
                        "attempt": 0,
                        "task_id": "task-budget",
                    },
                    idempotency_key=(
                        "phase:run-budget:cycle-budget:ROOT_CAUSE_ANALYSIS:"
                        "entry-fev-budget:artifact:phase-task"
                    ),
                ),
            ),
            gates=(),
        )

        with patch("cptr.services.factory_production.time.time", return_value=1_000):
            outcome = await handler.execute(context)

        self.assertEqual(outcome.next_state, FactoryState.PLANNING)
        self.assertTrue(outcome.artifacts[0].payload["budget_exhausted"])
        self.assertEqual(outcome.artifacts[0].payload["tool_calls"], 3)
        self.assertIn("2-tool budget", outcome.artifacts[0].payload["budget_reason"])
        self.assertIn("partial machine-guided finding", outcome.artifacts[0].payload["summary"])
        agent.cancel_task.assert_awaited_once_with("task-budget", user_id="user-1")
        agent.get_output.assert_not_awaited()

    async def test_baseline_prepares_isolated_mutation_lane_before_advisory(self):
        temp = self._git_repo()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        revision = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        assignment = SimpleNamespace(
            worker_id="worker-baseline",
            branch="cptr/direct/worker-baseline",
            base_revision=revision,
        )
        handler = BaselinePhaseHandler(
            workers=SimpleNamespace(),
            worker_store=SimpleNamespace(),
        )
        context = SimpleNamespace(
            run=SimpleNamespace(
                id="run-baseline",
                user_id="user-1",
                workspace_id="workspace-1",
                mission="prepare before advisory",
                acceptance_criteria=("smoke passes",),
                model_id="agent:model",
                policy={
                    "implementation_required": True,
                    "verification_targets": [
                        {
                            "gate_id": "smoke",
                            "phase": "full",
                            "target": "python_pytest",
                            "test_path": "test_smoke.py",
                            "category": "broader_tests",
                            "acceptance_ids": [1],
                        }
                    ],
                },
                budget={},
            ),
            cycle=SimpleNamespace(id="cycle-baseline", attempt_count=0),
            evidence=(),
            gates=(),
        )

        with (
            patch(
                "cptr.services.factory_production._repo_root", new=AsyncMock(return_value=str(root))
            ),
            patch(
                "cptr.services.factory_production.identity_for_user_id",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "cptr.services.factory_production._ensure_mutation_assignment",
                new=AsyncMock(return_value=assignment),
            ) as ensure_worker,
        ):
            outcome = await handler.execute(context)

        self.assertEqual(outcome.next_state, FactoryState.UNDERSTANDING)
        self.assertEqual(outcome.cycle_updates["mutation_worker_id"], "worker-baseline")
        self.assertEqual(outcome.target_revision, revision)
        self.assertTrue(any(item.kind == "factory_worker" for item in outcome.artifacts))
        ensure_worker.assert_awaited_once()

    async def test_reproduction_cleanup_restores_tracked_and_removes_untracked_scratch(self):
        temp = self._git_repo()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        original = (root / "test_smoke.py").read_text(encoding="utf-8")
        (root / "test_smoke.py").write_text("broken = True\n", encoding="utf-8")
        (root / "reproduce.py").write_text("print('scratch')\n", encoding="utf-8")

        await _reset_isolated_reproduction(root)

        self.assertEqual((root / "test_smoke.py").read_text(encoding="utf-8"), original)
        self.assertFalse((root / "reproduce.py").exists())
        status = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"], text=True
        )
        self.assertEqual(status, "")

    async def test_implementation_handoff_reuses_prior_evidence_and_tightens_budget(self):
        agent = SimpleNamespace(
            start_task=AsyncMock(return_value={"id": "task-implementation-handoff"})
        )
        handler = ImplementationPhaseHandler(
            workers=SimpleNamespace(),
            worker_store=SimpleNamespace(),
            agent=agent,
        )
        assignment = SimpleNamespace(worker_id="worker-1", base_revision="rev", branch="branch")
        context = SimpleNamespace(
            run=SimpleNamespace(
                id="run-implementation-handoff",
                user_id="user-1",
                workspace_id="workspace-1",
                mission="fix one defect",
                acceptance_criteria=("fix is verified",),
                model_id="agent:model",
                policy={
                    "implementation_required": True,
                    "implementation_timeout_seconds": 240,
                    "implementation_max_tool_calls": 36,
                },
            ),
            cycle=SimpleNamespace(
                id="cycle-implementation-handoff",
                attempt_count=0,
                mutation_worker_id="worker-1",
            ),
            evidence=(
                SimpleNamespace(
                    kind="reasoning_advice",
                    payload={
                        "phase_state": "REPRODUCING",
                        "summary": "change cptr/app.py health response and add a focused regression test",
                    },
                ),
            ),
            gates=(),
        )

        with (
            patch.object(handler, "_ensure_assignment", new=AsyncMock(return_value=assignment)),
            patch(
                "cptr.services.factory_production._factory_worker_workspace",
                new=AsyncMock(
                    return_value=(Path("/isolated"), SimpleNamespace(id="workspace-worker"))
                ),
            ),
        ):
            outcome = await handler.execute(context)

        prompt = agent.start_task.await_args.kwargs["prompt"]
        self.assertIn("PRIOR PHASE EVIDENCE", prompt)
        self.assertIn("[REPRODUCING] change cptr/app.py health response", prompt)
        self.assertIn("finish within 150 seconds", prompt)
        self.assertIn("at most 20 tool actions", prompt)
        payload = outcome.artifacts[0].payload
        self.assertEqual(payload["timeout_seconds"], 150)
        self.assertEqual(payload["max_tool_calls"], 20)
        self.assertGreater(payload["handoff_evidence_chars"], 0)

    async def test_implementation_budget_cancels_and_routes_to_machine_verification(self):
        running = {
            "id": "task-implementation",
            "status": "RUNNING",
            "created_at": 999_000,
            "raw_output": [
                {"type": "function_call", "call_id": f"call-{index}"} for index in range(8)
            ],
        }
        cancelled = {**running, "status": "CANCELLED"}
        agent = SimpleNamespace(
            get_task=AsyncMock(return_value=running),
            cancel_task=AsyncMock(return_value=cancelled),
        )
        handler = ImplementationPhaseHandler(
            workers=SimpleNamespace(),
            worker_store=SimpleNamespace(),
            agent=agent,
        )
        assignment = SimpleNamespace(worker_id="worker-1", base_revision="rev", branch="branch")
        context = SimpleNamespace(
            run=SimpleNamespace(
                id="run-implementation",
                user_id="user-1",
                workspace_id="workspace-1",
                mission="fix one defect",
                acceptance_criteria=("fix is verified",),
                model_id="agent:model",
                policy={
                    "implementation_required": True,
                    "implementation_timeout_seconds": 600,
                    "implementation_max_tool_calls": 8,
                },
            ),
            cycle=SimpleNamespace(
                id="cycle-implementation",
                attempt_count=0,
                mutation_worker_id="worker-1",
            ),
            evidence=(
                SimpleNamespace(
                    kind="factory_implementation_task",
                    payload={"attempt": 0, "task_id": "task-implementation"},
                ),
            ),
            gates=(),
        )

        with (
            patch.object(handler, "_ensure_assignment", new=AsyncMock(return_value=assignment)),
            patch(
                "cptr.services.factory_production._factory_worker_workspace",
                new=AsyncMock(
                    return_value=(Path("/isolated"), SimpleNamespace(id="workspace-worker"))
                ),
            ),
            patch("cptr.services.factory_production.time.time", return_value=1_000),
            patch(
                "cptr.services.factory_production._worker_mutation_target",
                new=AsyncMock(
                    return_value=(
                        "rev",
                        "fp-budget",
                        [{"status": "modified", "path": "cptr/app.py"}],
                    )
                ),
            ) as worker_target,
        ):
            outcome = await handler.execute(context)

        self.assertIsNone(outcome.failure)
        self.assertEqual(outcome.next_state, FactoryState.TARGETED_VERIFYING)
        self.assertEqual(outcome.target_revision, "rev")
        self.assertEqual(outcome.target_fingerprint, "fp-budget")
        self.assertTrue(outcome.artifacts[0].payload["budget_exhausted"])
        self.assertIn("8-tool budget", outcome.artifacts[0].payload["budget_reason"])
        self.assertEqual(outcome.artifacts[0].payload["changed_path_count"], 1)
        agent.cancel_task.assert_awaited_once_with("task-implementation", user_id="user-1")
        worker_target.assert_awaited_once_with(Path("/isolated"), "user-1")

    async def test_implementation_budget_rejects_missing_or_worker_committed_mutation(self):
        cases = (
            ("rev", [], "FACTORY_IMPLEMENTATION_NO_CHANGES"),
            (
                "rev-worker-commit",
                [{"status": "modified", "path": "cptr/app.py"}],
                "FACTORY_IMPLEMENTATION_REVISION_CHANGED",
            ),
        )
        for observed_revision, manifest, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                running = {
                    "id": "task-invalid-mutation",
                    "status": "RUNNING",
                    "created_at": 999_000,
                    "raw_output": [
                        {"type": "function_call", "call_id": f"call-{index}"} for index in range(8)
                    ],
                }
                cancelled = {**running, "status": "CANCELLED"}
                agent = SimpleNamespace(
                    get_task=AsyncMock(return_value=running),
                    cancel_task=AsyncMock(return_value=cancelled),
                )
                handler = ImplementationPhaseHandler(
                    workers=SimpleNamespace(),
                    worker_store=SimpleNamespace(),
                    agent=agent,
                )
                assignment = SimpleNamespace(
                    worker_id="worker-1", base_revision="rev", branch="branch"
                )
                context = SimpleNamespace(
                    run=SimpleNamespace(
                        id="run-invalid-mutation",
                        user_id="user-1",
                        workspace_id="workspace-1",
                        mission="fix one defect",
                        acceptance_criteria=("fix is verified",),
                        model_id="agent:model",
                        policy={
                            "implementation_required": True,
                            "implementation_timeout_seconds": 600,
                            "implementation_max_tool_calls": 8,
                        },
                    ),
                    cycle=SimpleNamespace(
                        id="cycle-invalid-mutation",
                        attempt_count=0,
                        mutation_worker_id="worker-1",
                    ),
                    evidence=(
                        SimpleNamespace(
                            kind="factory_implementation_task",
                            payload={"attempt": 0, "task_id": "task-invalid-mutation"},
                        ),
                    ),
                    gates=(),
                )

                with (
                    patch.object(
                        handler, "_ensure_assignment", new=AsyncMock(return_value=assignment)
                    ),
                    patch(
                        "cptr.services.factory_production._factory_worker_workspace",
                        new=AsyncMock(
                            return_value=(Path("/isolated"), SimpleNamespace(id="workspace-worker"))
                        ),
                    ),
                    patch("cptr.services.factory_production.time.time", return_value=1_000),
                    patch(
                        "cptr.services.factory_production._worker_mutation_target",
                        new=AsyncMock(return_value=(observed_revision, "fp-observed", manifest)),
                    ),
                ):
                    outcome = await handler.execute(context)

                self.assertIsNotNone(outcome.failure)
                self.assertEqual(outcome.failure.code, expected_code)
                self.assertIsNone(outcome.next_state)
                agent.cancel_task.assert_awaited_once_with(
                    "task-invalid-mutation", user_id="user-1"
                )

    async def test_transition_projection_next_action_stays_consistent(self):
        run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="keep projection consistent",
            acceptance_criteria=["state and next action agree"],
            policy={},
            budget={},
            model_id=None,
            idempotency_key="projection-consistency-run",
        )
        cycle = await self.store.create_cycle(
            run.id,
            base_revision=None,
            base_fingerprint=None,
            idempotency_key="projection-consistency-cycle",
        )
        await self.store.update_cycle_projection(
            run.id,
            cycle.id,
            updates={},
            run_next_action="wait for obsolete work",
            idempotency_key="projection-consistency-wait",
        )
        projected_run = await self.store.get_run(run.id)
        projected_cycle = await self.store.get_cycle(cycle.id)
        self.assertEqual(projected_run.next_action, "wait for obsolete work")
        self.assertEqual(projected_cycle.next_action, "wait for obsolete work")

        await self.store.transition(
            run.id,
            to_state=FactoryState.RECOVERING,
            actor=FactoryActor.SYSTEM,
            reason="advance",
            idempotency_key="projection-consistency-recovering",
        )
        recovered_run = await self.store.get_run(run.id)
        recovered_cycle = await self.store.get_cycle(cycle.id)
        self.assertIsNone(recovered_run.next_action)
        self.assertIsNone(recovered_cycle.next_action)

        await self.store.transition(
            run.id,
            to_state=FactoryState.BASELINING,
            actor=FactoryActor.SYSTEM,
            reason="baseline",
            idempotency_key="projection-consistency-baselining",
        )
        await self.store.transition(
            run.id,
            to_state=FactoryState.BLOCKED,
            actor=FactoryActor.SYSTEM,
            reason="source repository must remain clean",
            idempotency_key="projection-consistency-blocked",
        )
        blocked_run = await self.store.get_run(run.id)
        blocked_cycle = await self.store.get_cycle(cycle.id)
        self.assertEqual(blocked_run.next_action, "source repository must remain clean")
        self.assertEqual(blocked_cycle.next_action, "source repository must remain clean")
        self.assertIsNotNone(blocked_run.completed_at)

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

    async def test_terminal_run_quiesces_worker_ownership_before_runner_stops(self):
        run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="release writer ownership on terminal state",
            acceptance_criteria=["terminal run leaves no active writer"],
            policy={},
            budget={},
            model_id=None,
            idempotency_key="terminal-quiescence-run",
        )
        observed = SimpleNamespace(
            id=run.id,
            user_id="user-1",
            workspace_id="workspace-1",
            state=FactoryState.BLOCKED.value,
        )
        orchestrator = SimpleNamespace(run_once=AsyncMock(return_value=observed))
        worker_controller = SimpleNamespace(
            cancel_run=AsyncMock(
                return_value=SimpleNamespace(
                    quiescent=True,
                    unresolved_assignment_ids=(),
                    failed_command_ids=(),
                )
            )
        )
        worker_store = SimpleNamespace(list_terminal_blocking_run_ids=AsyncMock(return_value=[]))
        runner = FactoryProductionRunner(
            store=self.store,
            lease_ms=10_000,
            poll_interval=0.001,
            orchestrator=orchestrator,
            worker_store=worker_store,
            worker_controller=worker_controller,
            terminal_quiesce_timeout_ms=3210,
        )

        runner.schedule(run.id)
        await asyncio.sleep(0.01)
        await runner.close()

        orchestrator.run_once.assert_awaited_once_with(run.id)
        worker_controller.cancel_run.assert_awaited_once_with(observed, timeout_ms=3210)

    async def test_schedule_active_reconciles_terminal_writer_leases_after_restart(self):
        run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="recover terminal writer lease",
            acceptance_criteria=["startup releases stale writer ownership"],
            policy={},
            budget={},
            model_id=None,
            idempotency_key="terminal-restart-quiescence-run",
        )
        await self.store.transition(
            run.id,
            to_state=FactoryState.RECOVERING,
            actor=FactoryActor.SYSTEM,
            reason="recover",
            idempotency_key="terminal-restart-quiescence-recover",
        )
        await self.store.transition(
            run.id,
            to_state=FactoryState.BASELINING,
            actor=FactoryActor.SYSTEM,
            reason="baseline",
            idempotency_key="terminal-restart-quiescence-baseline",
        )
        await self.store.transition(
            run.id,
            to_state=FactoryState.BLOCKED,
            actor=FactoryActor.SYSTEM,
            reason="blocked",
            idempotency_key="terminal-restart-quiescence-blocked",
        )
        terminal = await self.store.get_run(run.id)
        worker_controller = SimpleNamespace(
            cancel_run=AsyncMock(
                return_value=SimpleNamespace(
                    quiescent=True,
                    unresolved_assignment_ids=(),
                    failed_command_ids=(),
                )
            )
        )
        worker_store = SimpleNamespace(
            list_terminal_blocking_run_ids=AsyncMock(return_value=[run.id])
        )
        runner = FactoryProductionRunner(
            store=self.store,
            lease_ms=10_000,
            poll_interval=0.001,
            orchestrator=SimpleNamespace(run_once=AsyncMock()),
            worker_store=worker_store,
            worker_controller=worker_controller,
        )

        scheduled = await runner.schedule_active()
        await runner.close()

        self.assertEqual(scheduled, [])
        worker_controller.cancel_run.assert_awaited_once()
        quiesced_run = worker_controller.cancel_run.await_args.args[0]
        self.assertEqual(quiesced_run.id, terminal.id)
        self.assertEqual(worker_controller.cancel_run.await_args.kwargs["timeout_ms"], 5000)

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
