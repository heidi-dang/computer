# MCP Durable Usage + Hybrid Coding Benchmark Design

## Goal

Make MCP-visible token/cost telemetry durable and queryable by week/month, and add a hybrid coding benchmark that records objective real-work engineering telemetry for every MCP session while providing standardized isolated benchmark runs for fair model-to-model comparison.

## Non-negotiable semantics

- Usage remains **Estimated · MCP-visible tokens** and **API-equivalent simulated cost**. It is never presented as the user's actual ChatGPT bill.
- Full prompt context, hidden reasoning, cache usage, and final-answer tokens remain unavailable to MCP and are not invented.
- Durable usage is idempotent by telemetry `event_id`; backend restarts must not double count.
- Real-work telemetry is operational evidence, not a scientifically comparable leaderboard score because tasks differ.
- The comparable leaderboard uses only standardized benchmark runs with server-owned grading and versioned suites.
- Standardized benchmark workspaces are disposable, owner-scoped, network-free by contract, and contain no hidden grader source.
- Benchmark scores are computed only from objective grader evidence. The model cannot self-score or submit a claimed score.

## Architecture

### Durable MCP usage

Add `mcp_usage_events` as the immutable source of truth. Each accepted usage diagnostic is projected through the reviewed pricing registry before persistence. A unique event ID provides restart-safe idempotency.

`McpUsageStore` exposes aggregation windows for current ISO week, current calendar month, rolling 7 days, rolling 30 days, and all time. Aggregation uses integer token counts and decimal text/SQL numeric conversion for simulated cost.

The diagnostics snapshot is enriched per authenticated admin with `usage_periods`. The existing 60-second ring remains the live chart source. Frontend reducers increment week/month aggregates from live usage events after the persisted snapshot is hydrated.

### Real-work engineering telemetry

Add `mcp_engineering_sessions`, keyed by owner + MCP session identity + model identity. Every newly persisted usage event updates observable counters:

- total/success/failed tool calls;
- coding mutation calls;
- read/investigation calls;
- verification/test/diff/status calls;
- estimated input/output tokens;
- simulated cost;
- first/last activity timestamps.

Expose an operational score with its components and formula, but mark `comparable=false`. The score rewards reliability and verification discipline and applies a bounded mutation-without-verification penalty. It does not claim code correctness.

### Standardized benchmark

Add `coding_benchmark_runs` and a versioned built-in suite. `start` creates an owner-scoped disposable Workspace under the CPTR data directory and writes only starter files plus the task README. The hidden grader implementation stays in backend code and uses a per-run random seed stored in the DB but not returned before grading.

Initial suite `cptr-python-core-v1` contains three deterministic Python tasks:

1. interval merge with unsorted/overlapping inputs;
2. bounded TTL/LRU cache semantics;
3. retry policy with retryable exception filtering and attempt accounting.

A run receives a fixed 100-point rubric: 30 + 35 + 35. `submit` executes the server-owned grader with a hard timeout, records per-case pass/fail evidence, final score, duration, and final status. The leaderboard groups completed runs by canonical model and suite version and reports attempts, best score, average score, pass rate, and median duration. No real-work score is mixed into this leaderboard.

### MCP tool surface

Expose four ChatGPT-facing tools through the MCP adapter:

- `cptr_benchmark_start`
- `cptr_benchmark_submit`
- `cptr_benchmark_get`
- `cptr_benchmark_leaderboard`

All tools accept the standard optional `client_model` field injected/reported by ChatGPT. Start forwards the current self-reported model to the backend. The returned benchmark workspace is then operated with existing Direct Coding tools, so the benchmark exercises the same MCP coding surface used in real work.

## Data model

### `mcp_usage_events`

Immutable columns: owner, event/session/client/model/tool identity, timestamp, estimated token counts, status, pricing status/version/rates, simulated input/output/total cost.

Indexes cover `(user_id, timestamp_ms)`, `(user_id, model_canonical, timestamp_ms)`, and `(user_id, session_id, timestamp_ms)`.

### `mcp_engineering_sessions`

Mutable aggregate row keyed by `(user_id, session_key, model_key)`. No prompts, tool arguments, tool results, source code, or chain-of-thought are stored.

### `coding_benchmark_runs`

Owner, model identity, suite/version, workspace ID/path reference, random grader seed, lifecycle timestamps, objective score, case evidence JSON, and failure summary. The seed is never returned before completion.

## API

- diagnostics ingest persists usage before publishing live usage events;
- diagnostics snapshot/stream snapshot includes `usage_periods`;
- `GET /api/mcp/engineering/sessions` returns real-work operational metrics;
- `POST /api/control/v1/benchmarks/runs` starts a standardized run;
- `POST /api/control/v1/benchmarks/runs/{run_id}/submit` grades and finalizes;
- `GET /api/control/v1/benchmarks/runs/{run_id}` returns one owned run;
- `GET /api/control/v1/benchmarks/leaderboard` returns comparable standardized results.

## UI

The existing usage card replaces process-lifetime headline totals with explicit **This week** and **This month** token/cost totals while retaining the 60-second charts. It also shows the selected period's input/output split and average simulated cost/request.

Add a compact **Coding benchmark** panel below usage analytics showing:

- latest standardized score and suite version;
- leaderboard rows by model;
- real-work session count and reliability/verification metrics;
- explicit labels distinguishing `Comparable standardized` from `Observed real-work`.

## Failure handling

- Duplicate usage IDs are ignored durably and are not re-emitted as new usage after restart.
- A usage DB failure returns a telemetry ingestion error rather than silently losing durable accounting.
- Benchmark grading timeout finalizes the run as `FAILED` with zero score and bounded public error text.
- A benchmark run can be submitted once; repeated submit returns the stored final result idempotently.
- Missing/deleted benchmark workspace fails closed.

## Verification

- migration upgrade/downgrade tests;
- usage idempotency + week/month boundary aggregation tests;
- real-work metric classification/formula tests;
- benchmark workspace isolation + randomized hidden grader tests;
- benchmark router ownership/idempotency tests;
- MCP adapter tool-contract/client tests;
- frontend type/reducer/static UI tests;
- backend Python test suite and frontend/plugin build/type/test gates.
