import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import (
    Base,
    FactoryCapabilityOutcome,
    FactoryCapabilityPerformance,
    FactoryCapabilityRecord,
    FactoryCycle,
    FactoryEvent,
    FactoryEvidence,
    FactoryGateResult,
    FactoryMetricProjection,
    FactoryReasoningCall,
    FactoryRun,
)
from cptr.services.factory_capabilities import (
    CapabilityManifest,
    CapabilityTrustStatus,
    CapabilityVerificationStatus,
)
from cptr.services.factory_domain import FactoryState
from cptr.services.factory_metrics import FactoryMetricsService
from cptr.services.factory_store import SqlFactoryStore


def _manifest(name: str = "metric-capability") -> CapabilityManifest:
    return CapabilityManifest(
        stable_id=name,
        version="1.0.0",
        origin_type="builtin",
        origin_uri=f"builtin://{name}",
        pinned_version_or_commit="1.0.0",
        digest="a" * 64,
        capabilities=("code.edit",),
        permissions=("workspace:write",),
        network_requirements=(),
        execution_requirements=("cptr-direct-coding",),
        risk_classification="low",
        trust_status=CapabilityTrustStatus.APPROVED,
        verification_status=CapabilityVerificationStatus.CAPABILITY_TESTED,
        maintenance_metadata={},
        historical_factory_score=None,
        created_at=1,
        evaluated_at=1,
    )


class FactoryMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlFactoryStore(session_factory=self.sessions)
        self.metrics = FactoryMetricsService(session_factory=self.sessions)
        self.manifest = _manifest()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _run_cycle(self, *, key: str, state: FactoryState = FactoryState.MISSION):
        run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="secret mission text must never enter metrics",
            acceptance_criteria=["secret acceptance criterion"],
            policy={"max_cycles": 1},
            budget={"max_repair_attempts_per_signature": 3},
            model_id="configured-model",
            idempotency_key=key,
        )
        cycle = await self.store.create_cycle(
            run.id,
            base_revision="base",
            base_fingerprint="base-fp",
            idempotency_key=f"{key}-cycle",
        )
        async with self.sessions() as db:
            persistent_run = await db.get(FactoryRun, run.id)
            persistent_cycle = await db.get(FactoryCycle, cycle.id)
            persistent_run.state = state.value
            persistent_run.current_cycle_id = cycle.id
            persistent_run.updated_at = 2_000
            persistent_cycle.state = state.value
            persistent_cycle.created_at = 1_000
            persistent_cycle.updated_at = 2_000
            persistent_cycle.attempt_count = 2
            persistent_cycle.failure_signatures = {
                "sig": {
                    "signature": "sig",
                    "category": "test",
                    "code": "TEST_FAILED",
                    "count": 2,
                    "last_seen_at": 1_500,
                }
            }
            persistent_cycle.selected_capabilities = [self.manifest.identity]
            db.add(
                FactoryCapabilityRecord(
                    id=self.manifest.identity,
                    stable_id=self.manifest.stable_id,
                    version=self.manifest.version,
                    origin_type=self.manifest.origin_type,
                    origin_uri=self.manifest.origin_uri,
                    pinned_version_or_commit=self.manifest.pinned_version_or_commit,
                    digest=self.manifest.digest,
                    capabilities=list(self.manifest.capabilities),
                    permissions=list(self.manifest.permissions),
                    network_requirements=list(self.manifest.network_requirements),
                    execution_requirements=list(self.manifest.execution_requirements),
                    risk_classification=self.manifest.risk_classification,
                    trust_status=self.manifest.trust_status.value,
                    verification_status=self.manifest.verification_status.value,
                    maintenance_metadata=dict(self.manifest.maintenance_metadata),
                    historical_factory_score_ppm=None,
                    created_at=1,
                    evaluated_at=1,
                )
            )
            db.add(
                FactoryReasoningCall(
                    run_id=run.id,
                    cycle_id=cycle.id,
                    role="IMPLEMENTER",
                    role_ordinal=1,
                    schema_id="factory.implementer.v1",
                    provider="openai",
                    model="configured-model",
                    response_id="resp-secret-not-needed",
                    input_tokens=100,
                    output_tokens=40,
                    total_tokens=140,
                    runtime_ms=250,
                    cost_microusd=12_000,
                    attempt_count=2,
                    data={"hidden_reasoning": "must not enter metrics"},
                    provider_metadata={"source": "must not enter metrics"},
                    created_at=1_100,
                )
            )
            db.add(
                FactoryGateResult(
                    run_id=run.id,
                    cycle_id=cycle.id,
                    gate_id="unit",
                    category="unit",
                    required=True,
                    applicable=True,
                    status="PASS",
                    evidence_ids=[],
                    evaluated_revision="target",
                    evaluated_fingerprint="target-fp",
                    reason="private gate prose",
                    attempt=1,
                    created_at=1_200,
                    updated_at=1_600,
                )
            )
            db.add(
                FactoryEvidence(
                    run_id=run.id,
                    cycle_id=cycle.id,
                    gate_id=None,
                    kind="capability_execution",
                    source="factory-execution-router",
                    authority="MACHINE",
                    revision="target",
                    fingerprint="target-fp",
                    digest="b" * 64,
                    payload={
                        "capability_identity": self.manifest.identity,
                        "runtime_ms": 75,
                        "input_tokens": 3,
                        "output_tokens": 2,
                        "cost_microusd": 100,
                        "output": "secret execution output",
                    },
                    created_at=1_300,
                )
            )
            await db.commit()
        return run, cycle

    async def _mark_machine_success(self, run_id: str, cycle_id: str):
        async with self.sessions() as db:
            run = await db.get(FactoryRun, run_id)
            cycle = await db.get(FactoryCycle, cycle_id)
            run.state = FactoryState.COMPLETE.value
            run.completed_at = 2_500
            run.updated_at = 2_500
            cycle.state = FactoryState.COMPLETE.value
            cycle.completed_at = 2_400
            cycle.updated_at = 2_400
            db.add(
                FactoryEvent(
                    run_id=run_id,
                    cycle_id=cycle_id,
                    sequence=999,
                    actor="SYSTEM",
                    event_type="victory.authorized",
                    from_state=FactoryState.VICTORY_JUDGING.value,
                    to_state=FactoryState.COMMITTING.value,
                    idempotency_key=f"victory-{run_id}",
                    payload_digest="c" * 64,
                    payload={
                        "cycle_id": cycle_id,
                        "satisfied_gate_ids": ["unit"],
                        "evaluated_revision": "target",
                        "evaluated_fingerprint": "target-fp",
                    },
                    created_at=2_000,
                )
            )
            await db.commit()

    async def test_numeric_projection_excludes_prompt_source_and_hidden_reasoning(self):
        run, _cycle = await self._run_cycle(key="numeric")

        summary = await self.metrics.refresh_run(
            run.id,
            repository_family="python",
            task_family="debugging",
        )

        encoded = repr(summary)
        self.assertNotIn("secret mission", encoded)
        self.assertNotIn("secret acceptance", encoded)
        self.assertNotIn("hidden_reasoning", encoded)
        self.assertNotIn("secret execution output", encoded)
        self.assertNotIn("private gate prose", encoded)
        self.assertEqual(summary["comparability"], "observed_real_work_only")
        self.assertNotIn("benchmark_score", summary)
        self.assertEqual(summary["run"]["input_tokens"], 103)
        self.assertEqual(summary["run"]["output_tokens"], 42)
        self.assertEqual(summary["run"]["runtime_ms"], 325)
        self.assertEqual(summary["run"]["repair_iterations"], 2)
        self.assertEqual(summary["roles"][0]["role"], "IMPLEMENTER")
        self.assertEqual(summary["gates"][0]["gate_latency_ms"], 600)

        async with self.sessions() as db:
            rows = list((await db.scalars(select(FactoryMetricProjection))).all())
        self.assertGreaterEqual(len(rows), 5)
        for row in rows:
            values = vars(row)
            self.assertNotIn("mission", values)
            self.assertNotIn("prompt", values)
            self.assertNotIn("source", values)
            self.assertNotIn("reasoning", values)

    async def test_failed_or_blocked_run_never_counts_as_verified_capability_success(self):
        run, _cycle = await self._run_cycle(key="blocked", state=FactoryState.BLOCKED)

        summary = await self.metrics.refresh_run(
            run.id,
            repository_family="python",
            task_family="debugging",
        )

        self.assertEqual(summary["run"]["verified_outcome"], "FAILURE")
        async with self.sessions() as db:
            outcomes = list((await db.scalars(select(FactoryCapabilityOutcome))).all())
            performance = list((await db.scalars(select(FactoryCapabilityPerformance))).all())
        self.assertEqual(outcomes, [])
        self.assertEqual(performance, [])

    async def test_machine_complete_run_records_capability_success_once_from_persisted_victory(
        self,
    ):
        run, cycle = await self._run_cycle(key="success")
        await self._mark_machine_success(run.id, cycle.id)

        first = await self.metrics.refresh_run(
            run.id,
            repository_family="python",
            task_family="debugging",
        )
        second = await self.metrics.refresh_run(
            run.id,
            repository_family="python",
            task_family="debugging",
        )

        self.assertEqual(first["run"]["verified_outcome"], "SUCCESS")
        self.assertEqual(second["run"]["verified_outcome"], "SUCCESS")
        async with self.sessions() as db:
            outcomes = list((await db.scalars(select(FactoryCapabilityOutcome))).all())
            performance = list((await db.scalars(select(FactoryCapabilityPerformance))).all())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(performance), 1)
        self.assertEqual(performance[0].attempts, 1)
        self.assertEqual(performance[0].verified_successes, 1)
        self.assertEqual(performance[0].verified_failures, 0)
        self.assertEqual(performance[0].repair_iterations, 2)

    async def test_caller_cannot_forge_verified_success_without_persisted_victory_proof(self):
        run, cycle = await self._run_cycle(key="forged", state=FactoryState.COMPLETE)
        async with self.sessions() as db:
            persistent_run = await db.get(FactoryRun, run.id)
            persistent_cycle = await db.get(FactoryCycle, cycle.id)
            persistent_run.completed_at = 2_500
            persistent_cycle.state = FactoryState.COMPLETE.value
            await db.commit()

        summary = await self.metrics.refresh_run(
            run.id,
            repository_family="python",
            task_family="debugging",
        )

        self.assertIsNone(summary["run"]["verified_outcome"])
        async with self.sessions() as db:
            self.assertEqual(list((await db.scalars(select(FactoryCapabilityOutcome))).all()), [])
            self.assertEqual(
                list((await db.scalars(select(FactoryCapabilityPerformance))).all()), []
            )

    async def test_longitudinal_summary_is_observational_and_separate_from_standardized_benchmarks(
        self,
    ):
        run, cycle = await self._run_cycle(key="longitudinal")
        await self._mark_machine_success(run.id, cycle.id)
        await self.metrics.refresh_run(
            run.id,
            repository_family="python",
            task_family="debugging",
        )

        summary = await self.metrics.longitudinal_summary(
            repository_family="python",
            task_family="debugging",
        )

        self.assertFalse(summary["comparable"])
        self.assertEqual(summary["comparability"], "observed_real_work_only")
        self.assertEqual(summary["capabilities"][0]["verified_success_rate"], 1.0)
        self.assertIn("regression_rate", summary["capabilities"][0])
        encoded = repr(summary).lower()
        self.assertNotIn("suite_id", encoded)
        self.assertNotIn("benchmark", encoded)


if __name__ == "__main__":
    unittest.main()
