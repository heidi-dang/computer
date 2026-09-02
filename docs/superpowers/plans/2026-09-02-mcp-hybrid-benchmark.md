# MCP Durable Usage + Hybrid Coding Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist MCP usage across restarts with weekly/monthly analytics and ship a hybrid coding benchmark combining per-session real-work evidence with isolated standardized, objectively graded benchmark runs.

**Architecture:** SQLite/Alembic stores immutable usage events, per-session engineering aggregates, and benchmark runs. Backend services own aggregation, scoring, isolation and hidden grading. The MCP adapter exposes benchmark lifecycle tools while existing direct coding tools operate the disposable benchmark workspace. The `/mcp` UI renders durable week/month usage and benchmark/real-work summaries.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy async + SQLite/Alembic, Svelte/TypeScript frontend, Node/TypeScript MCP adapter.

**Spec:** `docs/superpowers/specs/2026-09-02-mcp-hybrid-benchmark-design.md`

## Global Constraints

- Never label MCP-visible estimates as actual ChatGPT billing.
- Usage persistence must be idempotent by `event_id` across backend restarts.
- Real-work telemetry must remain `comparable=false` and store no prompts/source/tool payloads.
- Standardized leaderboard uses only server-owned objective grader results.
- Benchmark grader seed is private until completion and benchmark workspaces contain no hidden grader source.
- No new network dependency or package install.

---

### Task 1: Durable usage and real-work persistence

**Files:**
- Create: `cptr/models/metrics.py`
- Create: `cptr/migrations/versions/0018_mcp_usage_benchmarks.py`
- Create: `cptr/services/mcp_usage_store.py`
- Modify: `cptr/models/__init__.py`
- Modify: `cptr/routers/mcp.py`
- Modify: `cptr/services/mcp_diagnostics.py`
- Test: `tests/test_mcp_usage_persistence.py`

**Interfaces:**
- Produces `McpUsageStore.ingest(owner_id, events) -> set[str]` and `summary(owner_id, now_ms=None) -> dict`.
- Produces `engineering_sessions(owner_id, limit=...) -> dict`.
- Diagnostics snapshots gain `usage_periods`.

- [ ] Write failing persistence tests for restart-safe duplicate IDs, week/month windows, and session aggregate classification.
- [ ] Run the targeted tests and confirm failures are caused by missing models/service.
- [ ] Add the migration/models/service with SQL aggregation and bounded transparent operational score.
- [ ] Wire diagnostics ingestion to persist before live fan-out and filter durable duplicates.
- [ ] Enrich admin snapshot/SSE snapshot with persisted periods.
- [ ] Re-run targeted tests and existing MCP usage/diagnostics tests.

### Task 2: Standardized isolated benchmark backend

**Files:**
- Create: `cptr/services/coding_benchmark.py`
- Create: `cptr/routers/benchmarks.py`
- Modify: `cptr/app.py` or router registration module as required by existing pattern
- Reuse: `cptr/models/metrics.py`, migration 0018
- Test: `tests/test_coding_benchmark.py`
- Test: `tests/test_coding_benchmark_api.py`

**Interfaces:**
- `benchmark_store.start(owner_id, model_reported, suite_id) -> run dict`
- `benchmark_store.submit(owner_id, run_id) -> finalized run dict`
- `benchmark_store.get(owner_id, run_id) -> run | None`
- `benchmark_store.leaderboard(owner_id, suite_id) -> dict`

- [ ] Write failing tests proving workspace isolation, private random seed, three starter tasks, objective 100-point rubric, idempotent submission, ownership, and leaderboard grouping.
- [ ] Run tests and confirm expected missing-feature failures.
- [ ] Implement suite fixture creation under CPTR data dir and owner-scoped temporary Workspace registration.
- [ ] Implement server-owned randomized grader with hard timeout and bounded evidence.
- [ ] Implement lifecycle/leaderboard routes and register them.
- [ ] Re-run benchmark tests and DB migration tests.

### Task 3: Week/month and benchmark UI

**Files:**
- Modify: `cptr/frontend/src/lib/apis/mcp.ts`
- Modify: `cptr/frontend/src/lib/stores/mcp-diagnostics.ts`
- Modify: `cptr/frontend/src/lib/components/mcp/McpUsageCostPanel.svelte`
- Create: `cptr/frontend/src/lib/components/mcp/McpBenchmarkPanel.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpTopology.svelte`
- Test: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

**Interfaces:**
- `McpDiagnosticsSnapshot.usage_periods` contains `week`, `month`, `rolling_7d`, `rolling_30d`, `all_time`.
- UI fetches engineering/benchmark summary endpoints and renders standardized vs observed evidence separately.

- [ ] Add failing frontend static/reducer tests for `This week`, `This month`, removal of process-lifetime headline copy, durable period hydration/live increment, standardized score/leaderboard, and observed real-work labels.
- [ ] Run frontend targeted tests and confirm failures.
- [ ] Implement API types/reducer changes and responsive panels.
- [ ] Re-run frontend tests and build/type checks.

### Task 4: ChatGPT-facing MCP benchmark tools

**Files in MCP adapter repository:**
- Modify: `server/types.ts`
- Modify: `server/schemas/tools.ts`
- Modify: `server/client/computer-client.ts`
- Modify: `server/mcp.ts`
- Modify: `tests/mcp.test.ts`
- Add targeted benchmark client/contract test if useful.

**Interfaces:**
- `cptr_benchmark_start({suite_id?, client_model?})`
- `cptr_benchmark_submit({run_id, client_model?})`
- `cptr_benchmark_get({run_id, client_model?})`
- `cptr_benchmark_leaderboard({suite_id?, client_model?})`

- [ ] Add failing MCP contract tests proving all four tools exist, are direct ChatGPT tools, have bounded schemas, forward model identity on start, and do not require delegation authorization.
- [ ] Run targeted MCP tests and confirm failures.
- [ ] Add ComputerClient calls and tool registrations using the existing instrumentation wrapper so usage telemetry automatically covers benchmark calls.
- [ ] Re-run adapter tests, typecheck/build, and deployed-contract expectations.

### Task 5: Full verification and integration

**Files:** all changed files.

- [ ] Run backend targeted tests, full Python test suite, and migration head validation.
- [ ] Run frontend tests/build/typecheck.
- [ ] Run MCP adapter tests/typecheck/build.
- [ ] Inspect Git diffs for privacy leaks, hardcoded host paths, grader seed exposure, billing mislabeling, and source-checkout overlap.
- [ ] Integrate both model-free workers only when clean verification evidence is available.
- [ ] Report any deployment/restart step separately; do not claim deployment unless executed and verified.
