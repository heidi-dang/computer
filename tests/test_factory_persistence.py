import asyncio
import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.factory_domain import FactoryActor, FactoryState, InvalidFactoryTransition
from cptr.services.factory_gates import (
    EvidenceAuthority,
    FactoryGateCategory,
    FactoryGatePlan,
    FactoryGateSpec,
    FactoryGateStatus,
    GateEvidence,
    GateResult,
)
from cptr.services.factory_store import FactoryIdempotencyConflict, SqlFactoryStore
from cptr.services.factory_victory import FactoryVictoryJudge


class FactoryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlFactoryStore(session_factory=self.sessions)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_create_run_is_idempotent_and_preserves_immutable_input(self):
        first = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="Original mission",
            acceptance_criteria=["criterion-a", "criterion-b"],
            policy={"allow_network": False},
            budget={"max_cycles": 4},
            model_id="configured-model",
            idempotency_key="factory-run-1",
        )
        second = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="Changed mission",
            acceptance_criteria=["different"],
            policy={"allow_network": True},
            budget={"max_cycles": 99},
            model_id="other-model",
            idempotency_key="factory-run-1",
        )

        self.assertEqual(first.id, second.id)
        reloaded = await SqlFactoryStore(session_factory=self.sessions).get_run(first.id)
        self.assertEqual(reloaded.mission, "Original mission")
        self.assertEqual(reloaded.acceptance_criteria, ["criterion-a", "criterion-b"])
        self.assertEqual(reloaded.state, FactoryState.MISSION.value)

    async def test_transition_replay_is_idempotent_and_payload_mismatch_fails_closed(self):
        run = await self._create_run("transition-run")

        first = await self.store.transition(
            run.id,
            to_state=FactoryState.RECOVERING,
            actor=FactoryActor.SYSTEM,
            reason="startup recovery",
            idempotency_key="transition-1",
        )
        replay = await self.store.transition(
            run.id,
            to_state=FactoryState.RECOVERING,
            actor=FactoryActor.SYSTEM,
            reason="startup recovery",
            idempotency_key="transition-1",
        )

        self.assertEqual(first.state, FactoryState.RECOVERING.value)
        self.assertEqual(replay.state, FactoryState.RECOVERING.value)
        events = await self.store.list_events(run.id)
        transition_events = [event for event in events if event.event_type == "state.transition"]
        self.assertEqual(len(transition_events), 1)

        with self.assertRaises(FactoryIdempotencyConflict):
            await self.store.transition(
                run.id,
                to_state=FactoryState.BASELINING,
                actor=FactoryActor.SYSTEM,
                reason="different replay payload",
                idempotency_key="transition-1",
            )

    async def test_cycle_evidence_and_gate_round_trip(self):
        run = await self._create_run("evidence-run")
        cycle = await self.store.create_cycle(
            run.id,
            base_revision="abc123",
            base_fingerprint="fp-1",
            idempotency_key="cycle-1",
        )
        evidence = await self.store.append_evidence(
            run_id=run.id,
            cycle_id=cycle.id,
            gate_id="lint",
            kind="command",
            source="server_verifier",
            authority=EvidenceAuthority.MACHINE,
            revision="abc123",
            fingerprint="fp-1",
            payload={"argv": ["ruff", "check", "."], "exit_code": 0},
        )
        gate = await self.store.record_gate(
            run_id=run.id,
            cycle_id=cycle.id,
            gate_id="lint",
            category="lint",
            required=True,
            applicable=True,
            status="PASS",
            evidence_ids=[evidence.id],
            evaluated_revision="abc123",
            evaluated_fingerprint="fp-1",
            reason="command exited successfully",
            attempt=1,
        )

        cycles = await self.store.list_cycles(run.id)
        evidence_rows = await self.store.list_evidence(run.id)
        gate_rows = await self.store.list_gates(run.id, cycle_id=cycle.id)

        self.assertEqual([item.id for item in cycles], [cycle.id])
        self.assertEqual([item.id for item in evidence_rows], [evidence.id])
        self.assertEqual([item.id for item in gate_rows], [gate.id])
        self.assertEqual(gate_rows[0].evidence_ids, [evidence.id])
        self.assertEqual(evidence_rows[0].payload["exit_code"], 0)
        self.assertEqual(evidence_rows[0].authority, EvidenceAuthority.MACHINE.value)

    async def test_only_machine_issued_current_victory_can_authorize_commit_path(self):
        run = await self._create_run("victory-run")
        cycle = await self.store.create_cycle(
            run.id,
            base_revision="rev-1",
            base_fingerprint="fp-1",
            idempotency_key="victory-cycle",
        )
        await self.store.set_cycle_target(
            run.id,
            cycle.id,
            revision="rev-1",
            fingerprint="fp-1",
            idempotency_key="target-rev-1",
        )
        await self._advance_to_victory_judging(run.id)

        plan = FactoryGatePlan(
            specs=(
                FactoryGateSpec(
                    "acceptance",
                    FactoryGateCategory.ACCEPTANCE,
                    acceptance_ids=("criterion",),
                ),
            ),
            acceptance_criterion_ids=("criterion",),
        )
        evidence = GateEvidence(
            evidence_id="ev-acceptance",
            digest="digest-acceptance",
            authority=EvidenceAuthority.MACHINE,
            revision="rev-1",
            fingerprint="fp-1",
            kind="acceptance_probe",
            source="server_verifier",
        )
        decision = FactoryVictoryJudge().evaluate(
            gate_plan=plan,
            gate_results={
                "acceptance": GateResult(
                    gate_id="acceptance",
                    status=FactoryGateStatus.PASS,
                    evidence_ids=(evidence.evidence_id,),
                    reason="criterion passed",
                    evaluated_revision="rev-1",
                    evaluated_fingerprint="fp-1",
                )
            },
            evidence={evidence.evidence_id: evidence},
            current_revision="rev-1",
            current_fingerprint="fp-1",
        )

        with self.assertRaisesRegex(InvalidFactoryTransition, "machine Victory"):
            await self.store.transition(
                run.id,
                to_state=FactoryState.COMMITTING,
                actor=FactoryActor.SYSTEM,
                reason="attempt to bypass the Victory judge",
                idempotency_key="generic-victory-bypass",
            )

        with self.assertRaises(TypeError):
            await self.store.authorize_victory(
                run.id,
                cycle.id,
                {"passed": True},
                idempotency_key="fake-victory",
            )

        committed = await self.store.authorize_victory(
            run.id,
            cycle.id,
            decision,
            idempotency_key="real-victory",
        )
        self.assertEqual(committed.state, FactoryState.COMMITTING.value)

    async def test_stale_machine_victory_is_rejected_after_target_revision_changes(self):
        run = await self._create_run("stale-victory-run")
        cycle = await self.store.create_cycle(
            run.id,
            base_revision="rev-old",
            base_fingerprint="fp-old",
            idempotency_key="stale-cycle",
        )
        await self.store.set_cycle_target(
            run.id,
            cycle.id,
            revision="rev-new",
            fingerprint="fp-new",
            idempotency_key="target-new",
        )
        await self._advance_to_victory_judging(run.id)
        plan = FactoryGatePlan(
            specs=(FactoryGateSpec("lint", FactoryGateCategory.LINT),),
            acceptance_criterion_ids=(),
        )
        evidence = GateEvidence(
            evidence_id="ev-old",
            digest="digest-old",
            authority=EvidenceAuthority.MACHINE,
            revision="rev-old",
            fingerprint="fp-old",
            kind="command",
            source="server_verifier",
        )
        stale_decision = FactoryVictoryJudge().evaluate(
            gate_plan=plan,
            gate_results={
                "lint": GateResult(
                    gate_id="lint",
                    status=FactoryGateStatus.PASS,
                    evidence_ids=(evidence.evidence_id,),
                    reason="old lint",
                    evaluated_revision="rev-old",
                    evaluated_fingerprint="fp-old",
                )
            },
            evidence={evidence.evidence_id: evidence},
            current_revision="rev-old",
            current_fingerprint="fp-old",
        )
        self.assertTrue(stale_decision.passed)

        with self.assertRaisesRegex(ValueError, "stale"):
            await self.store.authorize_victory(
                run.id,
                cycle.id,
                stale_decision,
                idempotency_key="stale-victory",
            )

    async def test_only_one_concurrent_lease_claim_wins_and_expired_lease_can_be_reclaimed(self):
        run = await self._create_run("lease-run")
        now = 1_000_000
        results = await asyncio.gather(
            self.store.claim_run(run.id, lease_token="owner-a", now_ms=now, lease_ms=1000),
            self.store.claim_run(run.id, lease_token="owner-b", now_ms=now, lease_ms=1000),
        )
        self.assertEqual(sum(bool(result) for result in results), 1)

        winner = "owner-a" if results[0] else "owner-b"
        loser = "owner-b" if winner == "owner-a" else "owner-a"
        self.assertFalse(
            await self.store.claim_run(
                run.id,
                lease_token=loser,
                now_ms=now + 999,
                lease_ms=1000,
            )
        )
        self.assertTrue(
            await self.store.claim_run(
                run.id,
                lease_token=loser,
                now_ms=now + 1001,
                lease_ms=1000,
            )
        )

    async def test_recoverable_runs_exclude_terminal_states(self):
        active = await self._create_run("recover-active")
        terminal = await self._create_run("recover-terminal")
        await self.store.transition(
            terminal.id,
            to_state=FactoryState.CANCELLED,
            actor=FactoryActor.USER,
            reason="user stopped run",
            idempotency_key="cancel-terminal",
        )

        recoverable = await self.store.list_recoverable()

        self.assertIn(active.id, {item.id for item in recoverable})
        self.assertNotIn(terminal.id, {item.id for item in recoverable})

    async def test_event_sequences_are_monotonic_and_bounded_listing_is_cursor_based(self):
        run = await self._create_run("event-run")
        await self.store.transition(
            run.id,
            to_state=FactoryState.RECOVERING,
            actor=FactoryActor.SYSTEM,
            reason="recover",
            idempotency_key="event-transition-1",
        )
        await self.store.transition(
            run.id,
            to_state=FactoryState.BASELINING,
            actor=FactoryActor.SYSTEM,
            reason="baseline",
            idempotency_key="event-transition-2",
        )

        all_events = await self.store.list_events(run.id, limit=100)
        self.assertEqual(
            [event.sequence for event in all_events],
            sorted(event.sequence for event in all_events),
        )
        tail = await self.store.list_events(
            run.id,
            after_sequence=all_events[0].sequence,
            limit=1,
        )
        self.assertEqual(len(tail), 1)
        self.assertGreater(tail[0].sequence, all_events[0].sequence)

    async def _advance_to_victory_judging(self, run_id: str):
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
            FactoryState.FULL_VERIFYING,
            FactoryState.ADVERSARIAL_REVIEW,
            FactoryState.SECURITY_REVIEW,
            FactoryState.LIVE_VERIFYING,
            FactoryState.VICTORY_JUDGING,
        )
        for index, state in enumerate(chain):
            await self.store.transition(
                run_id,
                to_state=state,
                actor=FactoryActor.SYSTEM,
                reason=f"advance to {state.value}",
                idempotency_key=f"advance-{run_id}-{index}",
            )

    async def _create_run(self, idempotency_key: str):
        return await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission=f"mission {idempotency_key}",
            acceptance_criteria=["criterion"],
            policy={},
            budget={},
            model_id="configured-model",
            idempotency_key=idempotency_key,
        )


if __name__ == "__main__":
    unittest.main()
