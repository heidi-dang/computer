import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.factory_capabilities import (
    CapabilityManifest,
    CapabilityRequirement,
    CapabilityTrustStatus,
    CapabilityVerificationStatus,
)
from cptr.services.factory_capability_ranking import (
    CapabilityHistory,
    CapabilityRankingPolicy,
    SqlCapabilityHistoryStore,
    rank_capabilities,
)


def _manifest(
    name: str,
    *,
    trust: CapabilityTrustStatus = CapabilityTrustStatus.APPROVED,
    permissions=("workspace:read",),
    capabilities=("code-search",),
    verification: CapabilityVerificationStatus = CapabilityVerificationStatus.LOCAL,
    maintenance=None,
):
    return CapabilityManifest(
        stable_id=f"cap_{name}",
        version="1",
        origin_type="builtin",
        origin_uri=f"cptr:{name}",
        pinned_version_or_commit="1",
        digest=(name * 64)[:64],
        capabilities=tuple(capabilities),
        permissions=tuple(permissions),
        network_requirements=(),
        execution_requirements=("cptr",),
        risk_classification="CPTR_BUILTIN",
        trust_status=trust,
        verification_status=verification,
        maintenance_metadata=maintenance or {"freshness_score": 1.0, "maintenance_score": 1.0},
    )


class CapabilityRankingTests(unittest.TestCase):
    def setUp(self):
        self.requirements = (
            CapabilityRequirement.create(
                requirement_id="analysis",
                capabilities=["code-search"],
                required_permissions=["workspace:read"],
                network_allowed=False,
            ),
        )
        self.policy = CapabilityRankingPolicy(
            allowed_permissions=frozenset({"workspace:read"}),
            network_allowed=False,
        )

    def test_trust_ineligible_candidates_never_rank_even_with_perfect_history(self):
        statuses = (
            CapabilityTrustStatus.REJECTED,
            CapabilityTrustStatus.QUARANTINED,
            CapabilityTrustStatus.REVOKED,
            CapabilityTrustStatus.STALE_REVIEW_REQUIRED,
            CapabilityTrustStatus.DISCOVERED,
        )
        for status in statuses:
            with self.subTest(status=status):
                candidate = _manifest("unsafe", trust=status)
                ranked = rank_capabilities(
                    self.requirements,
                    [candidate],
                    {candidate.identity: CapabilityHistory.perfect(attempts=100)},
                    self.policy,
                )
                self.assertEqual(ranked, [])

    def test_excessive_permission_is_removed_before_scoring(self):
        candidate = _manifest(
            "writer",
            permissions=("workspace:read", "workspace:write"),
        )
        ranked = rank_capabilities(
            self.requirements,
            [candidate],
            {candidate.identity: CapabilityHistory.perfect(attempts=100)},
            self.policy,
        )
        self.assertEqual(ranked, [])

    def test_low_sample_history_is_confidence_damped(self):
        low = _manifest("low-sample")
        established = _manifest("established")
        history = {
            low.identity: CapabilityHistory.perfect(attempts=1),
            established.identity: CapabilityHistory(
                attempts=20,
                verified_successes=16,
                verified_failures=4,
                regressions=0,
                repair_iterations=4,
                input_tokens=100,
                output_tokens=50,
                runtime_ms=1000,
                cost_usd=0.01,
            ),
        }

        ranked = rank_capabilities(
            self.requirements,
            [low, established],
            history,
            self.policy,
        )

        self.assertEqual(ranked[0].manifest.stable_id, established.stable_id)
        low_rank = next(item for item in ranked if item.manifest.stable_id == low.stable_id)
        established_rank = next(
            item for item in ranked if item.manifest.stable_id == established.stable_id
        )
        self.assertLess(low_rank.history_confidence, established_rank.history_confidence)

    def test_ranking_is_deterministic_and_exposes_decomposed_scores(self):
        alpha = _manifest("alpha")
        beta = _manifest("beta")

        first = rank_capabilities(self.requirements, [beta, alpha], {}, self.policy)
        second = rank_capabilities(self.requirements, [alpha, beta], {}, self.policy)

        self.assertEqual(
            [item.manifest.identity for item in first],
            [item.manifest.identity for item in second],
        )
        self.assertEqual(
            [item.manifest.identity for item in first],
            sorted(item.manifest.identity for item in first),
        )
        self.assertIn("fit", first[0].components)
        self.assertIn("history", first[0].components)
        self.assertIn("maintenance", first[0].components)
        self.assertIn("freshness", first[0].components)


class CapabilityHistoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlCapabilityHistoryStore(session_factory=self.sessions)
        self.manifest = _manifest("history")

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_outcomes_require_machine_verified_factory_result(self):
        with self.assertRaisesRegex(ValueError, "machine-verified"):
            await self.store.record_capability_outcome(
                manifest=self.manifest,
                repository_family="python",
                task_family="debugging",
                verified_success=True,
                machine_verified=False,
                regression=False,
                repair_iterations=0,
                input_tokens=10,
                output_tokens=5,
                runtime_ms=100,
                cost_usd=0.01,
            )

        self.assertIsNone(
            await self.store.get_history(
                self.manifest.identity,
                repository_family="python",
                task_family="debugging",
            )
        )

    async def test_verified_outcomes_accumulate_objective_metrics_and_confidence(self):
        await self.store.record_capability_outcome(
            manifest=self.manifest,
            repository_family="python",
            task_family="debugging",
            verified_success=True,
            machine_verified=True,
            regression=False,
            repair_iterations=1,
            input_tokens=10,
            output_tokens=5,
            runtime_ms=100,
            cost_usd=0.01,
        )
        await self.store.record_capability_outcome(
            manifest=self.manifest,
            repository_family="python",
            task_family="debugging",
            verified_success=False,
            machine_verified=True,
            regression=True,
            repair_iterations=2,
            input_tokens=20,
            output_tokens=7,
            runtime_ms=200,
            cost_usd=0.02,
        )

        history = await self.store.get_history(
            self.manifest.identity,
            repository_family="python",
            task_family="debugging",
        )

        self.assertEqual(history.attempts, 2)
        self.assertEqual(history.verified_successes, 1)
        self.assertEqual(history.verified_failures, 1)
        self.assertEqual(history.regressions, 1)
        self.assertEqual(history.repair_iterations, 3)
        self.assertEqual(history.input_tokens, 30)
        self.assertEqual(history.output_tokens, 12)
        self.assertEqual(history.runtime_ms, 300)
        self.assertAlmostEqual(history.cost_usd, 0.03)
        self.assertGreater(history.confidence, 0)
        self.assertLess(history.confidence, 1)


if __name__ == "__main__":
    unittest.main()
