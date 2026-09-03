import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, FactoryEvent
from cptr.services.factory_domain import FactoryState
from cptr.services.factory_gates import EvidenceAuthority, FactoryGateCategory, FactoryGateStatus
from cptr.services.factory_orchestrator import FactoryOrchestrator
from cptr.services.factory_phases import (
    CycleCompletePhaseHandler,
    PhaseArtifact,
    PhaseContext,
    PhaseGateUpdate,
    PhaseOutcome,
    RecoveryPhaseHandler,
    VictoryJudgingPhaseHandler,
)
from cptr.services.factory_store import SqlFactoryStore


class _Handler:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    async def execute(self, context):
        self.calls += 1
        if callable(self.outcome):
            return self.outcome(context)
        return self.outcome


def _gate_plan():
    return {
        "acceptance_criterion_ids": ["criterion-1"],
        "specs": [
            {
                "gate_id": "acceptance-1",
                "category": "acceptance",
                "required": True,
                "applicable": True,
                "invalidated_by_mutation": True,
                "acceptance_ids": ["criterion-1"],
            }
        ],
    }


class FactoryOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlFactoryStore(session_factory=self.sessions)
        self.run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="execute the complete durable factory loop",
            acceptance_criteria=["criterion-1: the implementation is machine verified"],
            policy={"max_cycles": 1},
            budget={"max_repair_attempts_per_signature": 3},
            model_id="configured-model",
            idempotency_key="phase7-orchestrator",
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_complete_state_progression_executes_one_phase_action_per_run_once(self):
        next_state = {
            FactoryState.MISSION: FactoryState.RECOVERING,
            FactoryState.BASELINING: FactoryState.UNDERSTANDING,
            FactoryState.UNDERSTANDING: FactoryState.AUDITING,
            FactoryState.AUDITING: FactoryState.SELECTING_FINDING,
            FactoryState.SELECTING_FINDING: FactoryState.CAPABILITY_ANALYSIS,
            FactoryState.CAPABILITY_ANALYSIS: FactoryState.SKILL_DISCOVERY,
            FactoryState.SKILL_DISCOVERY: FactoryState.TRUST_EVALUATION,
            FactoryState.TRUST_EVALUATION: FactoryState.SKILL_SELECTION,
            FactoryState.SKILL_SELECTION: FactoryState.REPRODUCING,
            FactoryState.REPRODUCING: FactoryState.ROOT_CAUSE_ANALYSIS,
            FactoryState.ROOT_CAUSE_ANALYSIS: FactoryState.PLANNING,
            FactoryState.PLANNING: FactoryState.IMPLEMENTING,
            FactoryState.IMPLEMENTING: FactoryState.TARGETED_VERIFYING,
            FactoryState.TARGETED_VERIFYING: FactoryState.FULL_VERIFYING,
            FactoryState.FULL_VERIFYING: FactoryState.ADVERSARIAL_REVIEW,
            FactoryState.ADVERSARIAL_REVIEW: FactoryState.SECURITY_REVIEW,
            FactoryState.SECURITY_REVIEW: FactoryState.LIVE_VERIFYING,
            FactoryState.LIVE_VERIFYING: FactoryState.VICTORY_JUDGING,
            FactoryState.COMMITTING: FactoryState.PUSHING,
            FactoryState.PUSHING: FactoryState.CI_VERIFYING,
            FactoryState.CI_VERIFYING: FactoryState.CYCLE_COMPLETE,
        }
        handlers = {
            state: _Handler(PhaseOutcome(next_state=target, reason=f"{state.value} complete"))
            for state, target in next_state.items()
        }
        handlers[FactoryState.RECOVERING] = RecoveryPhaseHandler()
        handlers[FactoryState.BASELINING] = _Handler(
            PhaseOutcome(
                next_state=FactoryState.UNDERSTANDING,
                reason="baseline captured",
                cycle_updates={
                    "base_revision": "rev-base",
                    "base_fingerprint": "fp-base",
                    "gate_plan": _gate_plan(),
                },
            )
        )
        handlers[FactoryState.IMPLEMENTING] = _Handler(
            PhaseOutcome(
                next_state=FactoryState.TARGETED_VERIFYING,
                reason="implementation complete",
                target_revision="rev-target",
                target_fingerprint="fp-target",
            )
        )
        handlers[FactoryState.LIVE_VERIFYING] = _Handler(
            PhaseOutcome(
                next_state=FactoryState.VICTORY_JUDGING,
                reason="live verification complete",
                artifacts=(
                    PhaseArtifact(
                        key="acceptance-proof",
                        kind="verification_result",
                        source="deterministic-test-provider",
                        authority=EvidenceAuthority.MACHINE,
                        revision="rev-target",
                        fingerprint="fp-target",
                        payload={"passed": True, "tests": 1},
                    ),
                ),
                gates=(
                    PhaseGateUpdate(
                        gate_id="acceptance-1",
                        category=FactoryGateCategory.ACCEPTANCE,
                        required=True,
                        applicable=True,
                        status=FactoryGateStatus.PASS,
                        artifact_keys=("acceptance-proof",),
                        evaluated_revision="rev-target",
                        evaluated_fingerprint="fp-target",
                        reason="acceptance verified",
                        attempt=1,
                    ),
                ),
            )
        )
        handlers[FactoryState.VICTORY_JUDGING] = VictoryJudgingPhaseHandler()
        handlers[FactoryState.CYCLE_COMPLETE] = CycleCompletePhaseHandler()

        orchestrator = FactoryOrchestrator(
            store=self.store,
            handlers=handlers,
            owner_token="orchestrator-a",
            lease_ms=10_000,
        )

        observed = []
        for _ in range(40):
            before = await self.store.get_run(self.run.id)
            if before.state == FactoryState.COMPLETE.value:
                break
            after = await orchestrator.run_once(self.run.id)
            observed.append((before.state, after.state))
            self.assertNotEqual(before.state, after.state)

        completed = await self.store.get_run(self.run.id)
        self.assertEqual(completed.state, FactoryState.COMPLETE.value)
        self.assertIsNotNone(completed.completed_at)
        self.assertEqual(observed[0], (FactoryState.MISSION.value, FactoryState.RECOVERING.value))
        self.assertIn(
            (FactoryState.VICTORY_JUDGING.value, FactoryState.COMMITTING.value), observed
        )
        self.assertEqual(observed[-1], (FactoryState.CYCLE_COMPLETE.value, FactoryState.COMPLETE.value))
        self.assertTrue(all(handler.calls == 1 for handler in handlers.values() if isinstance(handler, _Handler)))

        events = await self.store.list_events(self.run.id, limit=200)
        transitions = [event for event in events if event.event_type in {"state.transition", "victory.authorized"}]
        self.assertEqual(len(transitions), len(observed))

    async def test_multi_cycle_advance_is_one_atomic_transition_not_create_then_transition(self):
        run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="run two durable cycles",
            acceptance_criteria=["cycle boundary survives restart"],
            policy={"max_cycles": 2},
            budget={},
            model_id="configured-model",
            idempotency_key="phase7-two-cycle",
        )
        cycle = await self.store.create_cycle(
            run.id,
            base_revision="rev-base",
            base_fingerprint="fp-base",
            idempotency_key="two-cycle-1",
        )
        async with self.sessions() as db:
            async with db.begin():
                persistent_run = await db.get(type(run), run.id)
                persistent_cycle = await db.get(type(cycle), cycle.id)
                persistent_run.state = FactoryState.CYCLE_COMPLETE.value
                persistent_run.current_cycle_id = cycle.id
                persistent_cycle.state = FactoryState.CYCLE_COMPLETE.value
                persistent_cycle.target_revision = "rev-cycle-1"
                persistent_cycle.target_fingerprint = "fp-cycle-1"
                db.add(
                    FactoryEvent(
                        id="fev-cycle-complete-entry",
                        run_id=run.id,
                        cycle_id=cycle.id,
                        sequence=3,
                        actor="SYSTEM",
                        event_type="state.transition",
                        from_state=FactoryState.CI_VERIFYING.value,
                        to_state=FactoryState.CYCLE_COMPLETE.value,
                        idempotency_key="seed-cycle-complete-entry",
                        payload_digest="0" * 64,
                        payload={},
                        created_at=1,
                    )
                )

        orchestrator = FactoryOrchestrator(
            store=self.store,
            handlers={FactoryState.CYCLE_COMPLETE: CycleCompletePhaseHandler()},
            owner_token="orchestrator-cycle-advance",
            lease_ms=10_000,
        )

        with patch.object(
            self.store,
            "transition",
            side_effect=AssertionError("cycle advance must not use a second transaction"),
        ):
            advanced = await orchestrator.run_once(run.id)

        self.assertEqual(advanced.state, FactoryState.AUDITING.value)
        cycles = await self.store.list_cycles(run.id)
        self.assertEqual([item.ordinal for item in cycles], [1, 2])
        self.assertEqual(advanced.current_cycle_id, cycles[1].id)
        self.assertEqual(cycles[0].state, FactoryState.CYCLE_COMPLETE.value)
        self.assertEqual(cycles[1].state, FactoryState.AUDITING.value)

    async def test_victory_rejects_gate_plan_that_does_not_cover_immutable_run_criteria(self):
        cycle = await self.store.create_cycle(
            self.run.id,
            base_revision="rev-base",
            base_fingerprint="fp-base",
            idempotency_key="victory-mismatch-cycle",
        )
        await self.store.update_cycle_projection(
            self.run.id,
            cycle.id,
            updates={"gate_plan": {"specs": [], "acceptance_criterion_ids": []}},
            run_next_action=None,
            idempotency_key="victory-mismatch-plan",
        )
        await self.store.set_cycle_target(
            self.run.id,
            cycle.id,
            revision="rev-target",
            fingerprint="fp-target",
            idempotency_key="victory-mismatch-target",
        )
        run = await self.store.get_run(self.run.id)
        cycle = await self.store.get_cycle(cycle.id)

        outcome = await VictoryJudgingPhaseHandler().execute(
            PhaseContext(run=run, cycle=cycle, evidence=(), gates=())
        )

        self.assertIsNotNone(outcome.failure)
        self.assertEqual(outcome.failure.code, "VICTORY_ACCEPTANCE_PLAN_MISMATCH")
        self.assertIsNone(outcome.victory_decision)

    async def test_foreign_live_lease_prevents_phase_provider_invocation(self):
        handler = _Handler(
            PhaseOutcome(next_state=FactoryState.RECOVERING, reason="mission accepted")
        )
        claimed = await self.store.claim_run(
            self.run.id,
            lease_token="other-owner",
            now_ms=1_000,
            lease_ms=1_000_000_000,
        )
        self.assertTrue(claimed)
        orchestrator = FactoryOrchestrator(
            store=self.store,
            handlers={FactoryState.MISSION: handler},
            owner_token="orchestrator-a",
            lease_ms=10_000,
            clock_ms=lambda: 2_000,
        )

        result = await orchestrator.run_once(self.run.id)

        self.assertEqual(result.state, FactoryState.MISSION.value)
        self.assertEqual(handler.calls, 0)

    async def test_phase_outcome_persists_artifacts_before_transition(self):
        handler = _Handler(
            PhaseOutcome(
                next_state=FactoryState.RECOVERING,
                reason="mission accepted from machine evidence",
                artifacts=(
                    PhaseArtifact(
                        key="mission-profile",
                        kind="mission_profile",
                        source="server",
                        authority=EvidenceAuthority.MACHINE,
                        payload={"accepted": True},
                    ),
                ),
            )
        )
        orchestrator = FactoryOrchestrator(
            store=self.store,
            handlers={FactoryState.MISSION: handler},
            owner_token="orchestrator-a",
            lease_ms=10_000,
        )

        result = await orchestrator.run_once(self.run.id)
        evidence = await self.store.list_evidence(self.run.id)
        events = await self.store.list_events(self.run.id)

        self.assertEqual(result.state, FactoryState.RECOVERING.value)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].kind, "mission_profile")
        transition = next(event for event in events if event.event_type == "state.transition")
        self.assertIn(evidence[0].id, transition.payload["evidence_ids"])


if __name__ == "__main__":
    unittest.main()
