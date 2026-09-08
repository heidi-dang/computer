import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    create_artifact,
)
from cptr.services.capability_os.evidence import EvidenceService
from cptr.services.capability_os.evolution import ChangeClass, EvolutionGate, PromotionDecision
from cptr.services.capability_os.evolution_engine import (
    EvolutionExperimentEngine,
    EvolutionExperimentError,
    ExperimentArmName,
    ExperimentContext,
    ExperimentMode,
)
from cptr.services.capability_os.store import SqlCapabilityOsStore


TASK_ID = "task-1"
CONTEXT = ExperimentContext(
    model_id="gpt-5.6-sol",
    reasoning_effort="high",
    tool_permission_fingerprint="perm-v1",
    resource_budget_fingerprint="budget-v1",
    task_distribution_fingerprint="holdout-v1",
)


class CapabilityOsEvolutionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityOsStore(session_factory=sessions)
        self.evidence = EvidenceService(store=self.store, clock_ms=lambda: 1_000_000)
        self.gate = EvolutionGate(min_runs_per_arm=2, max_regression_rate=0.1)
        self.experiments = EvolutionExperimentEngine(
            store=self.store,
            evidence=self.evidence,
            gate=self.gate,
            max_runs_per_arm=20,
        )
        control = create_artifact(
            artifact_id="tool.evolution.control",
            version="1",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec={"variant": "control"},
            created_at="2026-09-08T00:00:00Z",
            user_id="user-1",
            task_origin=TASK_ID,
            state=ArtifactState.QUALIFIED,
        )
        candidate = create_artifact(
            artifact_id="tool.evolution.candidate",
            version="2",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec={"variant": "candidate"},
            created_at="2026-09-08T00:00:01Z",
            user_id="user-1",
            task_origin=TASK_ID,
            state=ArtifactState.QUALIFIED,
        )
        await self.store.persist_artifact(control)
        await self.store.persist_artifact(candidate)
        self.control_digest = control.metadata.content_digest
        self.candidate_digest = candidate.metadata.content_digest

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _source(
        self,
        *,
        artifact_digest: str,
        success: bool = True,
        regression: bool = False,
        safety_events: int = 0,
        cost: float = 1.0,
        producer: str = "capability-vm",
        include_outcome: bool = True,
    ):
        claims = {"status": "pass", "verificationPassed": True}
        if include_outcome:
            claims["evolutionOutcome"] = {
                "success": success,
                "regression": regression,
                "safetyEvents": safety_events,
                "cost": cost,
            }
        return await self.evidence.record(
            task_id=TASK_ID,
            kind="capability.node",
            producer_identity=producer,
            claims=claims,
            artifact_digest=artifact_digest,
        )

    async def _create(self, *, change_class: ChangeClass = ChangeClass.INTERNAL):
        return await self.experiments.create(
            task_id=TASK_ID,
            change_class=change_class,
            mode=ExperimentMode.SHADOW,
            hypothesis="candidate improves verified success rate",
            control_artifact_digest=self.control_digest,
            candidate_artifact_digest=self.candidate_digest,
            context=CONTEXT,
            min_runs_per_arm=2,
        )

    async def test_evaluation_is_derived_from_server_owned_per_run_outcomes(self):
        plan, created_id = await self._create()
        c1 = await self._source(artifact_digest=self.control_digest, success=True, cost=10.0)
        c2 = await self._source(artifact_digest=self.control_digest, success=False, cost=12.0)
        n1 = await self._source(artifact_digest=self.candidate_digest, success=True, cost=8.0)
        n2 = await self._source(artifact_digest=self.candidate_digest, success=True, cost=8.0)

        for source in (c1, c2):
            await self.experiments.observe(
                task_id=TASK_ID,
                experiment_id=plan.experiment_id,
                arm=ExperimentArmName.CONTROL,
                source_evidence_id=source.evidence_id,
            )
        for source in (n1, n2):
            await self.experiments.observe(
                task_id=TASK_ID,
                experiment_id=plan.experiment_id,
                arm=ExperimentArmName.CANDIDATE,
                source_evidence_id=source.evidence_id,
            )

        evaluation = await self.experiments.evaluate(
            task_id=TASK_ID,
            experiment_id=plan.experiment_id,
        )
        self.assertEqual(evaluation.decision, PromotionDecision.PROMOTE)
        self.assertEqual((evaluation.control.runs, evaluation.control.successes), (2, 1))
        self.assertEqual((evaluation.candidate.runs, evaluation.candidate.successes), (2, 2))
        self.assertEqual(evaluation.control.mean_cost, 11.0)
        self.assertEqual(evaluation.candidate.mean_cost, 8.0)
        self.assertTrue(evaluation.evaluation_evidence_id.startswith("cevidence_"))

        status = await self.experiments.status(task_id=TASK_ID, experiment_id=plan.experiment_id)
        self.assertEqual(status["state"], "active")
        self.assertEqual(status["controlRuns"], 2)
        self.assertEqual(status["candidateRuns"], 2)
        self.assertEqual(status["latestDecision"], "promote")
        rows = await self.store.list_evidence_for_run(TASK_ID, plan.experiment_id)
        self.assertEqual(rows[0].evidence_id, created_id)
        observations = [row for row in rows if row.kind == "evolution.experiment.observation"]
        self.assertEqual(len(observations), 4)
        self.assertTrue(all(row.claims["outcomeSource"] == "server-evidence" for row in observations))

        with self.assertRaisesRegex(EvolutionExperimentError, "already counted"):
            await self.experiments.observe(
                task_id=TASK_ID,
                experiment_id=plan.experiment_id,
                arm=ExperimentArmName.CANDIDATE,
                source_evidence_id=n1.evidence_id,
            )

    async def test_observations_reject_client_labels_missing_server_outcome_and_wrong_arm(self):
        plan, _ = await self._create()
        client = await self._source(
            artifact_digest=self.control_digest,
            producer="capability-os-client:user-1",
        )
        with self.assertRaisesRegex(EvolutionExperimentError, "server-produced"):
            await self.experiments.observe(
                task_id=TASK_ID,
                experiment_id=plan.experiment_id,
                arm=ExperimentArmName.CONTROL,
                source_evidence_id=client.evidence_id,
            )

        missing = await self._source(
            artifact_digest=self.control_digest,
            include_outcome=False,
        )
        with self.assertRaisesRegex(EvolutionExperimentError, "server-owned evolutionOutcome"):
            await self.experiments.observe(
                task_id=TASK_ID,
                experiment_id=plan.experiment_id,
                arm=ExperimentArmName.CONTROL,
                source_evidence_id=missing.evidence_id,
            )

        wrong = await self._source(artifact_digest=self.candidate_digest)
        with self.assertRaisesRegex(EvolutionExperimentError, "wrong artifact arm"):
            await self.experiments.observe(
                task_id=TASK_ID,
                experiment_id=plan.experiment_id,
                arm=ExperimentArmName.CONTROL,
                source_evidence_id=wrong.evidence_id,
            )

    async def test_authority_critical_experiment_never_evaluates_to_auto_promote(self):
        plan, _ = await self._create(change_class=ChangeClass.AUTHORITY_CRITICAL)
        for arm, digest, success in (
            (ExperimentArmName.CONTROL, self.control_digest, False),
            (ExperimentArmName.CONTROL, self.control_digest, False),
            (ExperimentArmName.CANDIDATE, self.candidate_digest, True),
            (ExperimentArmName.CANDIDATE, self.candidate_digest, True),
        ):
            source = await self._source(artifact_digest=digest, success=success)
            await self.experiments.observe(
                task_id=TASK_ID,
                experiment_id=plan.experiment_id,
                arm=arm,
                source_evidence_id=source.evidence_id,
            )
        evaluation = await self.experiments.evaluate(task_id=TASK_ID, experiment_id=plan.experiment_id)
        self.assertEqual(evaluation.decision, PromotionDecision.OWNER_APPROVAL_REQUIRED)

    async def test_promotion_requires_reevaluation_after_newer_observation(self):
        plan, _ = await self._create()
        for arm, digest, success in (
            (ExperimentArmName.CONTROL, self.control_digest, False),
            (ExperimentArmName.CONTROL, self.control_digest, True),
            (ExperimentArmName.CANDIDATE, self.candidate_digest, True),
            (ExperimentArmName.CANDIDATE, self.candidate_digest, True),
        ):
            source = await self._source(artifact_digest=digest, success=success)
            await self.experiments.observe(
                task_id=TASK_ID,
                experiment_id=plan.experiment_id,
                arm=arm,
                source_evidence_id=source.evidence_id,
            )
        evaluation = await self.experiments.evaluate(
            task_id=TASK_ID,
            experiment_id=plan.experiment_id,
        )
        newer = await self._source(artifact_digest=self.candidate_digest, success=True)
        await self.experiments.observe(
            task_id=TASK_ID,
            experiment_id=plan.experiment_id,
            arm=ExperimentArmName.CANDIDATE,
            source_evidence_id=newer.evidence_id,
        )
        with self.assertRaisesRegex(EvolutionExperimentError, "reevaluation"):
            await self.experiments.record_promotion_intent(
                task_id=TASK_ID,
                experiment_id=plan.experiment_id,
                evaluation=evaluation,
                from_state="qualified",
                target_state="learned",
                owner_approval_verified=False,
            )

    async def test_cancel_closes_observation_and_evaluation_lifecycle(self):
        plan, _ = await self._create()
        cancel_id = await self.experiments.cancel(
            task_id=TASK_ID,
            experiment_id=plan.experiment_id,
            reason="candidate withdrawn",
        )
        self.assertTrue(cancel_id.startswith("cevidence_"))
        source = await self._source(artifact_digest=self.candidate_digest)
        with self.assertRaisesRegex(EvolutionExperimentError, "closed"):
            await self.experiments.observe(
                task_id=TASK_ID,
                experiment_id=plan.experiment_id,
                arm=ExperimentArmName.CANDIDATE,
                source_evidence_id=source.evidence_id,
            )
        with self.assertRaisesRegex(EvolutionExperimentError, "closed"):
            await self.experiments.evaluate(task_id=TASK_ID, experiment_id=plan.experiment_id)
        status = await self.experiments.status(task_id=TASK_ID, experiment_id=plan.experiment_id)
        self.assertEqual(status["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
