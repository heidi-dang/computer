import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.factory_domain import FactoryActor, FactoryState
from cptr.services.factory_gates import EvidenceAuthority, FactoryGateCategory, FactoryGateStatus
from cptr.services.factory_orchestrator import FactoryOrchestrator
from cptr.services.factory_phases import (
    PhaseArtifact,
    PhaseContext,
    PhaseFailure,
    PhaseFailureCategory,
    PhaseGateUpdate,
    PhaseOutcome,
    RepairRequiredPhaseHandler,
)
from cptr.services.factory_store import SqlFactoryStore


class _Handler:
    def __init__(self, fn_or_outcome):
        self.fn_or_outcome = fn_or_outcome
        self.calls = 0

    async def execute(self, context):
        self.calls += 1
        if callable(self.fn_or_outcome):
            return self.fn_or_outcome(context)
        return self.fn_or_outcome


def _failing_verification(summary: str):
    return PhaseOutcome(
        reason="targeted verification failed",
        artifacts=(
            PhaseArtifact(
                key="targeted-failure",
                gate_id="targeted-tests",
                kind="command_result",
                source="pytest",
                authority=EvidenceAuthority.MACHINE,
                revision="rev-target",
                fingerprint="fp-target",
                payload={"exit_code": 1, "summary": summary},
            ),
        ),
        gates=(
            PhaseGateUpdate(
                gate_id="targeted-tests",
                category=FactoryGateCategory.FOCUSED_TESTS,
                required=True,
                applicable=True,
                status=FactoryGateStatus.FAIL,
                artifact_keys=("targeted-failure",),
                evaluated_revision="rev-target",
                evaluated_fingerprint="fp-target",
                reason=summary,
                attempt=1,
            ),
        ),
        failure=PhaseFailure(
            category=PhaseFailureCategory.IMPLEMENTATION,
            code="TARGETED_TEST_FAILURE",
            gate_id="targeted-tests",
            summary=summary,
        ),
    )


class FactoryFailureLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlFactoryStore(session_factory=self.sessions)
        self.run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="repair a deterministic defect",
            acceptance_criteria=["the regression is fixed"],
            policy={},
            budget={"max_repair_attempts_per_signature": 3},
            model_id="configured-model",
            idempotency_key="phase7-failure-loop",
        )
        self.cycle = await self.store.create_cycle(
            self.run.id,
            base_revision="rev-base",
            base_fingerprint="fp-base",
            idempotency_key="cycle-1",
        )
        await self._transition_to_targeted_verifying()
        await self.store.set_cycle_target(
            self.run.id,
            self.cycle.id,
            revision="rev-target",
            fingerprint="fp-target",
            idempotency_key="target-1",
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _transition_to_targeted_verifying(self):
        chain = (
            FactoryState.RECOVERING,
            FactoryState.BASELINING,
            FactoryState.UNDERSTANDING,
            FactoryState.AUDITING,
            FactoryState.SELECTING_FINDING,
            FactoryState.CAPABILITY_ANALYSIS,
            FactoryState.SKILL_DISCOVERY,
            FactoryState.TRUST_EVALUATION,
            FactoryState.SKILL_SELECTION,
            FactoryState.REPRODUCING,
            FactoryState.ROOT_CAUSE_ANALYSIS,
            FactoryState.PLANNING,
            FactoryState.IMPLEMENTING,
            FactoryState.TARGETED_VERIFYING,
        )
        for index, state in enumerate(chain):
            await self.store.transition(
                self.run.id,
                to_state=state,
                actor=FactoryActor.SYSTEM,
                reason=f"test setup {state.value}",
                idempotency_key=f"setup:{index}:{state.value}",
            )

    async def _transition_repair_path_back_to_targeted(self):
        for index, state in enumerate(
            (
                FactoryState.PLANNING,
                FactoryState.IMPLEMENTING,
                FactoryState.TARGETED_VERIFYING,
            )
        ):
            await self.store.transition(
                self.run.id,
                to_state=state,
                actor=FactoryActor.SYSTEM,
                reason=f"repair path {state.value}",
                idempotency_key=f"repair-path:{index}:{state.value}:{self._testMethodName}",
            )

    async def test_gate_failure_is_persisted_then_debugged_and_reverified(self):
        failing = _Handler(_failing_verification("assertion failed at test_widget.py:42"))
        repair = RepairRequiredPhaseHandler(repeated_failure_threshold=2)
        targeted_success = _Handler(
            PhaseOutcome(
                next_state=FactoryState.FULL_VERIFYING,
                reason="targeted verification now passes",
                artifacts=(
                    PhaseArtifact(
                        key="targeted-pass",
                        gate_id="targeted-tests",
                        kind="command_result",
                        source="pytest",
                        authority=EvidenceAuthority.MACHINE,
                        revision="rev-target",
                        fingerprint="fp-target",
                        payload={"exit_code": 0, "tests": 1},
                    ),
                ),
                gates=(
                    PhaseGateUpdate(
                        gate_id="targeted-tests",
                        category=FactoryGateCategory.FOCUSED_TESTS,
                        required=True,
                        applicable=True,
                        status=FactoryGateStatus.PASS,
                        artifact_keys=("targeted-pass",),
                        evaluated_revision="rev-target",
                        evaluated_fingerprint="fp-target",
                        reason="targeted tests pass",
                        attempt=2,
                    ),
                ),
            )
        )
        orchestrator = FactoryOrchestrator(
            store=self.store,
            handlers={
                FactoryState.TARGETED_VERIFYING: failing,
                FactoryState.REPAIR_REQUIRED: repair,
            },
            owner_token="orchestrator-a",
            lease_ms=10_000,
        )

        failed = await orchestrator.run_once(self.run.id)
        self.assertEqual(failed.state, FactoryState.REPAIR_REQUIRED.value)
        cycle = await self.store.get_cycle(self.cycle.id)
        self.assertEqual(cycle.attempt_count, 1)
        self.assertEqual(len(cycle.failure_signatures), 1)
        failure_record = next(iter(cycle.failure_signatures.values()))
        self.assertEqual(failure_record["count"], 1)

        evidence = await self.store.list_evidence(self.run.id)
        self.assertTrue(any(item.kind == "command_result" for item in evidence))
        gates = await self.store.list_gates(self.run.id, cycle_id=self.cycle.id)
        self.assertEqual(gates[-1].status, FactoryGateStatus.FAIL.value)

        diagnosed = await orchestrator.run_once(self.run.id)
        self.assertEqual(diagnosed.state, FactoryState.ROOT_CAUSE_ANALYSIS.value)

        await self._transition_repair_path_back_to_targeted()
        orchestrator = FactoryOrchestrator(
            store=self.store,
            handlers={FactoryState.TARGETED_VERIFYING: targeted_success},
            owner_token="orchestrator-b",
            lease_ms=10_000,
        )
        verified = await orchestrator.run_once(self.run.id)
        self.assertEqual(verified.state, FactoryState.FULL_VERIFYING.value)
        gates = await self.store.list_gates(self.run.id, cycle_id=self.cycle.id)
        self.assertEqual([item.status for item in gates if item.gate_id == "targeted-tests"], ["FAIL", "PASS"])

    async def test_interrupted_failure_phase_replays_without_duplicate_evidence_or_attempts(self):
        failing = _Handler(_failing_verification("assertion failed before crash"))
        orchestrator = FactoryOrchestrator(
            store=self.store,
            handlers={FactoryState.TARGETED_VERIFYING: failing},
            owner_token="orchestrator-a",
            lease_ms=10_000,
        )
        original_transition = self.store.transition

        async def crash_before_transition(*_args, **_kwargs):
            raise RuntimeError("simulated process loss after durable phase writes")

        with patch.object(self.store, "transition", side_effect=crash_before_transition):
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                await orchestrator.run_once(self.run.id)

        after_crash = await self.store.get_cycle(self.cycle.id)
        self.assertEqual(after_crash.attempt_count, 1)
        self.assertEqual(len(await self.store.list_gates(self.run.id, cycle_id=self.cycle.id)), 1)

        with patch.object(self.store, "transition", wraps=original_transition):
            resumed = await orchestrator.run_once(self.run.id)

        self.assertEqual(resumed.state, FactoryState.REPAIR_REQUIRED.value)
        after_resume = await self.store.get_cycle(self.cycle.id)
        self.assertEqual(after_resume.attempt_count, 1)
        self.assertEqual(len(await self.store.list_gates(self.run.id, cycle_id=self.cycle.id)), 1)
        evidence = await self.store.list_evidence(self.run.id)
        self.assertEqual(sum(item.kind == "command_result" for item in evidence), 1)
        self.assertEqual(sum(item.kind == "phase_failure" for item in evidence), 1)

    async def test_repeated_identical_failure_escalates_to_capability_analysis(self):
        summaries = iter(
            [
                "assertion failed at /tmp/worktree/test_widget.py:42",
                "assertion failed at /tmp/another/test_widget.py:99",
            ]
        )
        failing = _Handler(lambda _context: _failing_verification(next(summaries)))
        repair = RepairRequiredPhaseHandler(repeated_failure_threshold=2)
        orchestrator = FactoryOrchestrator(
            store=self.store,
            handlers={
                FactoryState.TARGETED_VERIFYING: failing,
                FactoryState.REPAIR_REQUIRED: repair,
            },
            owner_token="orchestrator-a",
            lease_ms=10_000,
        )

        await orchestrator.run_once(self.run.id)
        first_repair = await orchestrator.run_once(self.run.id)
        self.assertEqual(first_repair.state, FactoryState.ROOT_CAUSE_ANALYSIS.value)
        await self._transition_repair_path_back_to_targeted()

        await orchestrator.run_once(self.run.id)
        second_repair = await orchestrator.run_once(self.run.id)

        self.assertEqual(second_repair.state, FactoryState.CAPABILITY_ANALYSIS.value)
        cycle = await self.store.get_cycle(self.cycle.id)
        self.assertEqual(cycle.attempt_count, 2)
        self.assertEqual(len(cycle.failure_signatures), 1)
        failure_record = next(iter(cycle.failure_signatures.values()))
        self.assertEqual(failure_record["count"], 2)
        self.assertEqual(failure_record["category"], "implementation")
        self.assertEqual(failure_record["code"], "TARGETED_TEST_FAILURE")

    async def test_configured_repair_budget_exhaustion_blocks_identical_signature(self):
        failure = _failing_verification("persistent deterministic failure").failure
        self.assertIsNotNone(failure)
        for attempt in range(1, 4):
            await self.store.record_failure(
                self.run.id,
                self.cycle.id,
                signature=failure.signature,
                category=failure.category.value,
                code=failure.code,
                gate_id=failure.gate_id,
                summary=failure.summary,
                idempotency_key=f"budget-failure:{attempt}",
            )
        run = await self.store.get_run(self.run.id)
        cycle = await self.store.get_cycle(self.cycle.id)

        outcome = await RepairRequiredPhaseHandler(repeated_failure_threshold=2).execute(
            PhaseContext(run=run, cycle=cycle, evidence=(), gates=())
        )

        self.assertEqual(outcome.next_state, FactoryState.BLOCKED)
        self.assertIn("repair budget", outcome.reason.lower())
        self.assertIn("3", outcome.reason)


if __name__ == "__main__":
    unittest.main()
