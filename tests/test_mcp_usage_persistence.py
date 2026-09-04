import importlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models.base import Base
from cptr.services.mcp_diagnostics import McpUsageDiagnostic


def ts(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def usage(
    event_id: str,
    *,
    timestamp_ms: int,
    tool_name: str,
    status: str = "complete",
    session_id: str = "mcp-session-1",
    input_tokens: int = 100,
    output_tokens: int = 20,
) -> McpUsageDiagnostic:
    return McpUsageDiagnostic(
        event_id=event_id,
        timestamp_ms=timestamp_ms,
        request_id=f"request-{event_id}",
        correlation_id=f"corr-{event_id}",
        session_id=session_id,
        client_id="chatgpt",
        model_reported="GPT-5.6 Sol",
        model_canonical="gpt-5.6-sol",
        model_source="self_reported",
        tool_name=tool_name,
        input_tokens_estimated=input_tokens,
        output_tokens_estimated=output_tokens,
        cached_input_tokens_estimated=None,
        estimator_method="output=o200k_base:fallback;input=o200k_base:fallback",
        estimator_exact_for_model=False,
        status=status,
    )


class McpUsagePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            module = importlib.import_module("cptr.services.mcp_usage_store")
            importlib.import_module("cptr.models.metrics")
        except ImportError as exc:
            self.fail(f"durable MCP usage persistence module is missing: {exc}")
        self.McpUsageStore = module.McpUsageStore
        self.temp = tempfile.TemporaryDirectory()
        database = Path(self.temp.name) / "usage.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = self.McpUsageStore(session_factory=self.factory)

    async def asyncTearDown(self):
        if hasattr(self, "engine"):
            await self.engine.dispose()
        if hasattr(self, "temp"):
            self.temp.cleanup()

    async def test_ingest_is_restart_safe_idempotent_and_aggregates_week_month(self):
        events = [
            usage(
                "usage-old-0001",
                timestamp_ms=ts("2026-08-30T12:00:00"),
                tool_name="cptr_code_read_file",
                input_tokens=10,
                output_tokens=2,
            ),
            usage(
                "usage-week-001",
                timestamp_ms=ts("2026-08-31T12:00:00"),
                tool_name="cptr_code_edit_file",
                input_tokens=100,
                output_tokens=20,
            ),
            usage(
                "usage-month-01",
                timestamp_ms=ts("2026-09-01T12:00:00"),
                tool_name="cptr_workspace_run_test_target",
                input_tokens=200,
                output_tokens=40,
            ),
            usage(
                "usage-month-02",
                timestamp_ms=ts("2026-09-02T12:00:00"),
                tool_name="cptr_code_read_file",
                status="error",
                input_tokens=300,
                output_tokens=60,
            ),
        ]

        accepted = await self.store.ingest("user-1", events)
        self.assertEqual(accepted, {event.event_id for event in events})

        # New service instance simulates a backend restart. Replaying the same
        # diagnostic IDs must not create durable double counting.
        restarted = self.McpUsageStore(session_factory=self.factory)
        replayed = await restarted.ingest("user-1", events)
        self.assertEqual(replayed, set())

        summary = await restarted.summary("user-1", now_ms=ts("2026-09-02T15:00:00"))
        self.assertEqual(summary["week"]["requests"], 3)
        self.assertEqual(summary["week"]["total_tokens_estimated"], 720)
        self.assertEqual(summary["month"]["requests"], 2)
        self.assertEqual(summary["month"]["total_tokens_estimated"], 600)
        self.assertEqual(summary["all_time"]["requests"], 4)
        self.assertEqual(summary["all_time"]["total_tokens_estimated"], 732)
        self.assertGreater(float(summary["week"]["simulated_cost_usd"]), 0)
        self.assertEqual(summary["timezone"], "UTC")

    async def test_same_batch_duplicate_usage_is_counted_once(self):
        event = usage(
            "usage-same-batch-duplicate",
            timestamp_ms=ts("2026-09-02T09:00:00"),
            tool_name="cptr_code_read_file",
        )
        accepted = await self.store.ingest("user-1", [event, event])
        self.assertEqual(accepted, {event.event_id})
        result = await self.store.engineering_sessions("user-1", limit=10)
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(result["sessions"][0]["tool_calls"], 1)

    async def test_batched_usage_ingest_bounds_sqlite_statement_pressure(self):
        statements: list[str] = []

        def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(str(statement).lstrip().split(None, 1)[0].upper())

        sqlalchemy_event.listen(self.engine.sync_engine, "before_cursor_execute", record_statement)
        try:
            events = [
                usage(
                    f"usage-batch-{index:03}",
                    timestamp_ms=ts("2026-09-02T10:00:00") + index,
                    tool_name="cptr_code_read_file",
                )
                for index in range(20)
            ]
            accepted = await self.store.ingest("user-1", events)
        finally:
            sqlalchemy_event.remove(self.engine.sync_engine, "before_cursor_execute", record_statement)

        self.assertEqual(len(accepted), 20)
        # One batched usage INSERT, one engineering-session SELECT, and one
        # engineering-session INSERT/flush are sufficient for a same-session batch.
        # Keep a small allowance for SQLite/SQLAlchemy bookkeeping without
        # regressing to O(events) write/query amplification.
        self.assertLessEqual(len(statements), 5, statements)
        self.assertLessEqual(statements.count("SELECT"), 1, statements)
        self.assertLessEqual(statements.count("INSERT"), 2, statements)

    async def test_real_work_session_metrics_are_observable_and_not_comparable(self):
        events = [
            usage(
                "usage-real-001",
                timestamp_ms=ts("2026-09-02T10:00:00"),
                tool_name="cptr_code_read_file",
            ),
            usage(
                "usage-real-002",
                timestamp_ms=ts("2026-09-02T10:01:00"),
                tool_name="cptr_code_edit_file",
            ),
            usage(
                "usage-real-003",
                timestamp_ms=ts("2026-09-02T10:02:00"),
                tool_name="cptr_workspace_run_test_target",
            ),
            usage(
                "usage-real-004",
                timestamp_ms=ts("2026-09-02T10:03:00"),
                tool_name="cptr_code_get_git_status",
                status="error",
            ),
        ]
        await self.store.ingest("user-1", events)

        result = await self.store.engineering_sessions("user-1", limit=10)
        self.assertEqual(result["comparability"], "observed_real_work_only")
        self.assertFalse(result["comparable"])
        self.assertEqual(len(result["sessions"]), 1)
        session = result["sessions"][0]
        self.assertEqual(session["session_id"], "mcp-session-1")
        self.assertEqual(session["model_canonical"], "gpt-5.6-sol")
        self.assertEqual(session["tool_calls"], 4)
        self.assertEqual(session["successful_tool_calls"], 3)
        self.assertEqual(session["failed_tool_calls"], 1)
        self.assertEqual(session["coding_mutations"], 1)
        self.assertEqual(session["verification_calls"], 2)
        self.assertEqual(session["read_calls"], 1)
        self.assertAlmostEqual(session["reliability"], 0.75)
        self.assertGreater(session["operational_score"], 0)
        self.assertLessEqual(session["operational_score"], 100)
        self.assertIn("not comparable", result["disclaimer"].lower())


if __name__ == "__main__":
    unittest.main()
