import unittest
from datetime import date
from decimal import Decimal

from pydantic import ValidationError

from cptr.services.mcp_diagnostics import McpDiagnosticsStore, McpUsageDiagnostic
from cptr.services.mcp_pricing import (
    PRICING_REGISTRY_VERSION,
    normalize_pricing_model,
    project_usage_cost,
)

BASE_TS = 1_788_000_000_000


def usage(
    event_id: str = "usage-0001",
    *,
    reported: str | None = "GPT-5.6 Sol",
    canonical: str | None = "gpt-5.6-sol",
    input_tokens: int = 1_000_000,
    output_tokens: int = 1_000_000,
    timestamp_ms: int = BASE_TS,
) -> McpUsageDiagnostic:
    return McpUsageDiagnostic(
        event_id=event_id,
        timestamp_ms=timestamp_ms,
        request_id="request-1",
        correlation_id="corr-1",
        session_id="session-1",
        client_id="chatgpt",
        model_reported=reported,
        model_canonical=canonical,
        model_source="self_reported" if reported else "unavailable",
        tool_name="cptr_list_workspaces",
        input_tokens_estimated=input_tokens,
        output_tokens_estimated=output_tokens,
        cached_input_tokens_estimated=None,
        estimator_method="output=o200k_base:fallback;input=o200k_base:fallback",
        estimator_exact_for_model=False,
        status="complete",
    )


class McpUsagePricingTests(unittest.TestCase):
    def test_gpt_5_6_sol_uses_exact_reviewed_rates_and_decimal_math(self):
        projected = project_usage_cost(usage(), today=date(2026, 9, 2))

        self.assertEqual(projected["pricing_status"], "current")
        self.assertEqual(projected["pricing_version"], PRICING_REGISTRY_VERSION)
        self.assertEqual(Decimal(projected["input_usd_per_million"]), Decimal("4.00"))
        self.assertEqual(Decimal(projected["cached_input_usd_per_million"]), Decimal("0.40"))
        self.assertEqual(Decimal(projected["output_usd_per_million"]), Decimal("20.00"))
        self.assertEqual(Decimal(projected["input_cost_usd"]), Decimal("4.00"))
        self.assertEqual(Decimal(projected["output_cost_usd"]), Decimal("20.00"))
        self.assertEqual(Decimal(projected["simulated_cost_usd"]), Decimal("24.00"))
        self.assertIsNone(projected["cached_input_tokens_estimated"])
        self.assertIsNone(projected["cached_input_cost_usd"])

    def test_model_lookup_is_exact_and_missing_unknown_stale_are_explicit(self):
        self.assertEqual(normalize_pricing_model("GPT-5.6 Sol", None), "gpt-5.6-sol")
        self.assertEqual(normalize_pricing_model("gpt-5.6", None), "gpt-5.6-sol")
        self.assertIsNone(normalize_pricing_model("mystery-gpt-5.6-special", None))

        missing = project_usage_cost(usage(reported=None, canonical=None), today=date(2026, 9, 2))
        self.assertEqual(missing["pricing_status"], "model_not_reported")
        self.assertIsNone(missing["simulated_cost_usd"])

        unknown = project_usage_cost(
            usage(reported="mystery-gpt-5.6-special", canonical=None),
            today=date(2026, 9, 2),
        )
        self.assertEqual(unknown["pricing_status"], "unknown_model")
        self.assertIsNone(unknown["simulated_cost_usd"])

        stale = project_usage_cost(usage(), today=date(2026, 11, 22))
        self.assertEqual(stale["pricing_status"], "stale")
        self.assertEqual(stale["pricing_valid_through"], "2026-11-21")
        self.assertEqual(Decimal(stale["simulated_cost_usd"]), Decimal("24.00"))

    def test_usage_schema_is_strict_and_accepts_counts_only(self):
        payload = usage().model_dump()
        for forbidden in ("arguments_json", "result_json", "authorization", "stack"):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValidationError):
                McpUsageDiagnostic.model_validate({**payload, forbidden: "must-not-survive"})


class McpUsageStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_ring_is_bounded_deduped_and_latest_model_is_not_sticky(self):
        store = McpDiagnosticsStore(
            max_latency_samples_per_edge=2,
            max_failures=2,
            max_system_samples=2,
            max_usage=2,
            subscriber_queue_size=2,
        )
        first = usage("usage-0001", input_tokens=100, output_tokens=10, timestamp_ms=BASE_TS)
        second = usage(
            "usage-0002",
            reported="GPT-5.6 Terra",
            canonical="gpt-5.6-terra",
            input_tokens=200,
            output_tokens=20,
            timestamp_ms=BASE_TS + 1,
        )
        latest_missing = usage(
            "usage-0003",
            reported=None,
            canonical=None,
            input_tokens=300,
            output_tokens=30,
            timestamp_ms=BASE_TS + 2,
        )
        result = await store.ingest([first, first, second, latest_missing])
        self.assertEqual(result, {"accepted": 3, "duplicates": 1, "dropped": 0})

        snapshot = await store.snapshot()
        self.assertEqual(
            [item["event_id"] for item in snapshot["usage"]], ["usage-0002", "usage-0003"]
        )
        self.assertEqual(snapshot["stream_health"]["usage_capacity"], 2)
        self.assertEqual(snapshot["current_model"]["pricing_status"], "model_not_reported")
        self.assertIsNone(snapshot["current_model"]["model_reported"])
        # Totals are cumulative for the backend process even after the bounded
        # Usage event ring evicts the oldest event.
        self.assertEqual(snapshot["usage_totals"]["input_tokens_estimated"], 600)
        self.assertEqual(snapshot["usage_totals"]["output_tokens_estimated"], 60)
        self.assertEqual(snapshot["usage_totals"]["total_tokens_estimated"], 660)
        self.assertEqual(snapshot["usage_totals"]["priced_events"], 2)
        self.assertEqual(snapshot["usage_totals"]["unpriced_events"], 1)
        self.assertIn("gpt-5.6-sol", snapshot["usage_totals"]["by_model"])
        self.assertIn("gpt-5.6-terra", snapshot["usage_totals"]["by_model"])


if __name__ == "__main__":
    unittest.main()
