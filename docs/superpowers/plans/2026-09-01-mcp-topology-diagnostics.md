# MCP Topology Diagnostics + Persistent Aliases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `/mcp` with server-persistent topology aliases, truthful per-edge latency, safe correlated error diagnostics, and a live CPTR backend system monitor, then complete the frontend warning cleanup and push the fully verified Computer and plugin histories to `main` without touching unrelated dirty work.

**Architecture:** Preserve the existing privacy split: `/api/mcp/traffic/*` remains metadata-only and `/api/mcp/activity/*` remains bounded/redacted tool input/output. Add a third bounded diagnostics path for latency, structured failures, and backend metrics; add a small persistent topology-config service backed by the existing `Config` model; propagate one opaque correlation ID through Traffic, Activity, and Diagnostics; and keep the ChatGPT-specific fallback identity change at the ChatGPT-facing adapter boundary rather than globally relabeling unknown clients. The frontend combines the three authenticated snapshot/SSE streams and persistent aliases to render selectable clients/infrastructure/edges, error drill-down, and backend health.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, asyncio, SQLAlchemy-backed existing `Config`, Linux `/proc` + platform-native system probes, optional existing `nvidia-smi`, Svelte 5, TypeScript, Node test runner, SSE, `@modelcontextprotocol/sdk`, existing CPTR bearer/admin authentication, existing redaction helpers.

**Spec:** `docs/superpowers/specs/2026-09-01-mcp-topology-diagnostics-design.md`

## Global Constraints

- Traffic topology remains metadata-only: no raw arguments, results, prompts, headers, secrets, or arbitrary exception text may enter `/api/mcp/traffic/*`.
- Activity remains the only channel that may carry bounded/redacted tool input/output/error payloads.
- Diagnostics may carry only bounded allowlisted operational fields and sanitized summaries; no bearer tokens, cookies, OAuth tokens, raw headers, stack traces, chain-of-thought, or unredacted sensitive paths.
- ChatGPT internet RTT must not be fabricated. The client-facing edge is labeled `Observed request time`; only controlled server-side hops may be labeled handoff latency or backend RTT/API latency.
- The ChatGPT-specific fallback is changed in `chatgpt-computer-plugin/server/index.ts`; generic `normalizeMcpClient(undefined)` continues to produce `Unknown MCP Client` for unrelated/generic use.
- Persistent aliases change presentation only. Canonical IDs, routing, auth, telemetry IDs, request IDs, session IDs, and correlation IDs never change because of an alias.
- Alias writes and all topology/diagnostics reads require admin authorization. Plugin diagnostics ingestion uses a dedicated bearer scope, `mcp:diagnostics:write`.
- All event rings, latency windows, metric histories, dedupe structures, and SSE subscriber queues are bounded.
- System/GPU/I/O probe failures must never affect MCP execution. Unsupported metrics render `Unavailable`, never `0` unless zero was actually measured.
- No new external monitoring service, database, telemetry vendor, graph framework, or package dependency.
- Frontend warning-cleanup acceptance is part of the release gate: zero actionable Svelte build warnings, zero ineffective dynamic-import warnings, zero oversized initial/shared chunk warnings under the approved partitioning policy, and zero `PLUGIN_TIMINGS` diagnostics.
- Preserve the current 69-action ChatGPT-visible MCP contract unless the latest remote main intentionally changes that contract before release; diagnostics work itself must not add/remove/rename MCP actions.
- Preserve existing NightOwl/mobile `/mcp` behavior and 44 px touch targets.
- The primary `computer/main` checkout contains unrelated dirty work and must not be reset, cleaned, overwritten, or used for release commits.
- Computer implementation source is `fix/mcp-mobile-runtime`; plugin implementation source is `feature/mcp-live-activity`.
- Final release candidates are created from freshly fetched `origin/main` in separate clean release worktrees/branches and are tested before `git push origin HEAD:main`.
- The user explicitly requested commit and push to `main`; no production deployment is implied.

---

## Execution harness preflight

Fresh Git worktrees do not carry ignored local runtimes. Before running Computer commands in any Computer source/worker/release worktree, reuse the existing primary checkout's ignored runtime without installing or modifying dependencies:

```bash
COMPUTER_MAIN="$(git worktree list --porcelain | awk '/^worktree /{p=substr($0,10)} p ~ /\/computer$/ {print p; exit}')"
test -n "$COMPUTER_MAIN"
test -x "$COMPUTER_MAIN/.venv/bin/python"
test -d "$COMPUTER_MAIN/cptr/frontend/node_modules"
test -e .venv || ln -s "$COMPUTER_MAIN/.venv" .venv
test -e cptr/frontend/node_modules || ln -s "$COMPUTER_MAIN/cptr/frontend/node_modules" cptr/frontend/node_modules
```

Before running plugin commands in any plugin source/worker/release worktree, reuse the primary plugin checkout's ignored dependencies:

```bash
PLUGIN_MAIN="$(git worktree list --porcelain | awk '/^worktree /{p=substr($0,10)} p ~ /\/chatgpt-computer-plugin$/ {print p; exit}')"
test -n "$PLUGIN_MAIN"
test -d "$PLUGIN_MAIN/node_modules"
test -e node_modules || ln -s "$PLUGIN_MAIN/node_modules" node_modules
```

These symlinks are test harness artifacts only. Never stage them; remove any symlink created solely by this plan before the final clean-tree gate.

---

## File map

### `computer`

- Existing warning-cleanup work: follow `docs/superpowers/plans/2026-09-01-frontend-build-warning-cleanup.md` and integrate reviewed worker commits before final release.
- Create `cptr/services/mcp_topology_config.py` — canonical topology labels, alias sanitization, persistent Config read/merge/reset.
- Create `tests/test_mcp_topology_config.py` — persistence, bounds, canonical-ID invariants.
- Create `cptr/services/mcp_diagnostics.py` — strict diagnostics schemas, latency aggregation, failure/system rings, bounded subscribers.
- Create `tests/test_mcp_diagnostics.py` — schema, bounds, percentiles, dedupe, redaction projection, subscriber behavior.
- Create `cptr/services/system_metrics.py` — non-blocking live system counter collection, rate derivation, optional NVIDIA probe, sampler loop.
- Create `tests/test_system_metrics.py` — deterministic CPU/RAM/disk/network/GPU/rate tests and unavailable capability tests.
- Modify `cptr/services/mcp_traffic.py` — optional `correlation_id` only; keep payload metadata-only.
- Modify `cptr/services/mcp_activity.py` — optional `correlation_id` only; keep payload bounds/redaction contract.
- Modify `cptr/routers/mcp.py` — topology config GET/PUT, diagnostics POST/snapshot/SSE, sampler start, existing Traffic/Activity correlation support.
- Modify `cptr/routers/gateway.py` — add `mcp:diagnostics:write` to the existing control credential scopes.
- Modify `cptr/app.py` — exact POST-only cookie-auth bypass for `/api/mcp/diagnostics/events`, matching Traffic/Activity ingestion.
- Create `tests/test_mcp_diagnostics_api.py` — scope/admin/middleware/SSE/config API tests.
- Modify `tests/test_mcp_activity_api.py` and `tests/test_mcp_traffic_api.py` — correlation remains safe and cross-channel.
- Modify `cptr/frontend/src/lib/apis/mcp.ts` — config/diagnostics types, snapshot/SSE helpers, correlation fields.
- Create `cptr/frontend/src/lib/stores/mcp-diagnostics.ts` — pure bounded diagnostics reducer/helpers.
- Create `cptr/frontend/src/lib/stores/mcp-topology.ts` — canonical labels, alias projection, topology selection types.
- Modify `cptr/frontend/src/lib/stores/mcp-traffic.ts` — carry `correlationId` into request rows.
- Modify `cptr/frontend/src/lib/stores/mcp-activity.ts` — carry `correlationId` and support focused request reveal.
- Modify `cptr/frontend/src/lib/components/mcp/McpTopology.svelte` — hydrate aliases/diagnostics and coordinate selection/error reveal.
- Modify `cptr/frontend/src/lib/components/mcp/McpTopologyGraph.svelte` — selectable infrastructure/edges, aliases, latency labels, health/error state.
- Create `cptr/frontend/src/lib/components/mcp/McpTopologyDetail.svelte` — shared alias editor and node/edge detail shell.
- Create `cptr/frontend/src/lib/components/mcp/McpBackendMonitor.svelte` — CPU/RAM/disk/I/O/network/GPU/process/subscriber monitor.
- Create `cptr/frontend/src/lib/components/mcp/McpDiagnosticDetail.svelte` — safe failure detail + Activity correlation action.
- Modify `cptr/frontend/src/lib/components/mcp/McpRecentRequests.svelte` — error stage/diagnostic detail and correlation action.
- Modify `cptr/frontend/src/lib/components/mcp/McpActivityFeed.svelte` — focus/reveal a correlated request without clearing history.
- Modify `cptr/frontend/src/lib/components/mcp/McpConsole.svelte` — pass focused request into Activity.
- Modify `cptr/frontend/src/routes/mcp/+page.svelte` — route-level Topology → Console correlation handoff.
- Modify `cptr/frontend/tests/mcp-traffic-topology.test.mjs` — aliases, latency, backend monitor, diagnostics, mobile/accessibility contracts.

### `chatgpt-computer-plugin`

- Create `server/mcp-diagnostics.ts` — strict diagnostic/latency event types and bounded best-effort queue/batcher.
- Modify `server/mcp-traffic.ts` — add opaque `correlationId` to request context/events while preserving generic unknown-client normalization.
- Modify `server/mcp-activity.ts` — add optional `correlation_id` to Activity events.
- Modify `server/client/computer-client.ts` — diagnostics ingestion plus a generic bounded backend-request observer around the existing request method.
- Modify `server/index.ts` — ChatGPT-specific fallback, correlation creation, controlled hop timings, diagnostics emitter wiring and request-boundary failure projection.
- Modify `server/mcp.ts` — propagate correlation and emit safe `cptr_mcp` diagnostics for tool-handler failures from the existing registration boundary.
- Create `tests/mcp-diagnostics.test.ts` — fallback, queue, correlation, timing, structured failure, delivery isolation.
- Modify `tests/mcp-traffic.test.ts`, `tests/mcp-activity.test.ts`, and `tests/mcp.test.ts` — correlation and unchanged visible tool contract.
- Modify `scripts/check-mcp-live-activity-integration.mjs` — real SDK acceptance includes Diagnostics and safe induced failure.
- Modify `package.json` only if a separate named acceptance script is useful; do not add dependencies.

---

### Task 0: Finish and integrate the existing frontend warning cleanup

**Files:**
- Existing plan: `docs/superpowers/plans/2026-09-01-frontend-build-warning-cleanup.md`
- Existing gate: `cptr/frontend/scripts/check-production-build.mjs`
- Existing source changes are isolated in the runes, form-accessibility, interaction-accessibility, and bundle-cleanup workers created from `636d9fb617912342181a6a08aa4fa48953f16e51`.

**Interfaces:**
- Produces a Computer integration branch where `npm run build:clean` exits 0.
- Produces reviewed warning-cleanup commits that can be merged with topology diagnostics work.
- Does not change MCP observability semantics.

- [ ] **Step 1: Re-open the warning-cleanup plan and inspect every current worker diff**

For each warning worker, review `git diff` and ensure its changed paths match its assigned warning class. Reject blanket `svelte-ignore`, broad `onwarn`, or arbitrary multi-megabyte chunk-limit changes.

- [ ] **Step 2: Finish any worker that is not independently GREEN**

For each worker, run the production build and verify its assigned warning count reaches zero. The runes and form lanes must also keep the existing frontend regressions green. The interaction lane must use semantic controls/keyboard behavior. The bundle lane must remove both ineffective dynamic imports and split initial/shared monoliths using supported Rolldown partitioning.

- [ ] **Step 3: Commit each independently reviewed worker change**

Use one commit per warning class:

```bash
git add cptr/frontend
git commit -m "fix: complete Svelte 5 warning cleanup"
```

```bash
git add cptr/frontend
git commit -m "fix: repair frontend accessibility warnings"
```

```bash
git add cptr/frontend
git commit -m "build: partition frontend bundles cleanly"
```

Do not stage generated `.fdx/` content or `node_modules` symlinks.

- [ ] **Step 4: Integrate the reviewed worker commits onto `fix/mcp-mobile-runtime`**

Because the source branch advanced beyond worker base `636d9fb`, cherry-pick the reviewed worker commits in dependency-safe order rather than forcing a stale mechanical integration. Resolve only overlapping formatting/accessibility edits; preserve all MCP feature behavior.

- [ ] **Step 5: Verify the exact integrated warning gate**

Run from `cptr/frontend`:

```bash
node --test tests/*.mjs
npm run build:clean
npx prettier --check src tests scripts package.json
```

Then from repository root:

```bash
git diff --check
git status --short
```

Expected: frontend tests pass, `build:clean` exits 0 with none of its forbidden signatures, Prettier exits 0, diff hygiene is clean, and only intentionally untracked generated `.fdx/` content may remain.

---

### Task 1: Add server-persistent topology aliases

**Files:**
- Create: `cptr/services/mcp_topology_config.py`
- Create: `tests/test_mcp_topology_config.py`
- Modify: `cptr/routers/mcp.py`

**Interfaces:**
- Produces `CANONICAL_TOPOLOGY_LABELS: dict[str, str]` with fixed infrastructure defaults.
- Produces `sanitize_topology_node_id(value: str) -> str`.
- Produces `sanitize_topology_alias(value: str | None) -> str | None`.
- Produces `async get_topology_config() -> dict[str, object]`.
- Produces `async update_topology_aliases(updates: dict[str, str | None]) -> dict[str, object]`.
- API: `GET /api/mcp/topology/config` and `PUT /api/mcp/topology/config` with body `{ "aliases": { "node-id": "Alias" | null } }`.

- [ ] **Step 1: Write failing service tests**

Create concrete tests that patch `Config.get`/`Config.upsert` and prove:

```python
CANONICAL_TOPOLOGY_LABELS == {
    "mcp-connector": "MCP Connector",
    "cptr-mcp": "CPTR MCP",
    "cptr-backend": "CPTR Backend",
}
```

Also prove:

```python
assert sanitize_topology_alias("  Workstation   Backend  ") == "Workstation Backend"
assert sanitize_topology_alias("   ") is None
with self.assertRaises(ValueError):
    sanitize_topology_alias("x" * 81)
with self.assertRaises(ValueError):
    sanitize_topology_node_id("../bad")
```

`update_topology_aliases({"cptr-backend": "Workstation"})` must call:

```python
Config.upsert({"mcp.topology.aliases": {"cptr-backend": "Workstation"}})
```

and a later update with `{"cptr-backend": None}` must remove only that alias.

- [ ] **Step 2: Run the service test and verify RED**

```bash
.venv/bin/python -m pytest tests/test_mcp_topology_config.py -q
```

Expected: import failure because the service does not exist.

- [ ] **Step 3: Implement the minimal persistent config service**

Use these exact constants and bounds:

```python
CONFIG_KEY = "mcp.topology.aliases"
MAX_ALIAS_LENGTH = 80
NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
CANONICAL_TOPOLOGY_LABELS = {
    "mcp-connector": "MCP Connector",
    "cptr-mcp": "CPTR MCP",
    "cptr-backend": "CPTR Backend",
}
```

Reject any ASCII control character (`ord(char) < 32` or `ord(char) == 127`) before normalization. Then normalize display whitespace with `" ".join(value.split())`; reject strings longer than 80 characters instead of silently changing user intent.

`get_topology_config()` returns:

```python
{
    "version": 1,
    "canonical_labels": CANONICAL_TOPOLOGY_LABELS,
    "aliases": aliases,
}
```

- [ ] **Step 4: Add admin-only router endpoints and API tests**

Add:

```python
class McpTopologyConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aliases: dict[str, str | None]
```

Both endpoints call `require_admin(request)`. `PUT` applies a partial merge/reset through `update_topology_aliases` and returns the resulting config.

- [ ] **Step 5: Run tests and commit**

```bash
.venv/bin/python -m pytest tests/test_mcp_topology_config.py -q
ruff check cptr/services/mcp_topology_config.py cptr/routers/mcp.py tests/test_mcp_topology_config.py
ruff format --check cptr/services/mcp_topology_config.py cptr/routers/mcp.py tests/test_mcp_topology_config.py
git diff --check
git add cptr/services/mcp_topology_config.py cptr/routers/mcp.py tests/test_mcp_topology_config.py
git commit -m "feat: persist MCP topology aliases"
```

---

### Task 2: Add the bounded diagnostics contract, correlation fields, and latency aggregation

**Files:**
- Create: `cptr/services/mcp_diagnostics.py`
- Create: `tests/test_mcp_diagnostics.py`
- Modify: `cptr/services/mcp_traffic.py`
- Modify: `cptr/services/mcp_activity.py`
- Modify: `tests/test_mcp_traffic.py`
- Modify: `tests/test_mcp_activity.py`

**Interfaces:**
- Traffic and Activity add `correlation_id: str | None`, max 128, without adding unsafe payloads.
- Produces `McpLatencySample`, `McpFailureDiagnostic`, `McpGpuMetrics`, `McpBackendMetricsSample`, `McpDiagnosticsBatch`, `McpDiagnosticsStore`, singleton `mcp_diagnostics_store`.
- `await McpDiagnosticsStore.ingest(events: list[McpLatencySample | McpFailureDiagnostic]) -> dict[str, int]`.
- `await McpDiagnosticsStore.record_system_sample(sample: McpBackendMetricsSample) -> None`.
- `await McpDiagnosticsStore.snapshot() -> dict[str, object]`.
- `subscribe()/unsubscribe()` follow the existing bounded Traffic/Activity pattern.

- [ ] **Step 1: Write failing diagnostics schema/store tests**

Use these exact enums:

```python
LatencyEdge = Literal[
    "client-mcp-connector",
    "mcp-connector-cptr-mcp",
    "cptr-mcp-cptr-backend",
]
LatencyMetric = Literal[
    "observed_request_time",
    "adapter_handoff",
    "backend_api_rtt",
]
FailureStage = Literal[
    "client_transport",
    "mcp_connector",
    "cptr_mcp",
    "cptr_backend",
    "activity_delivery",
    "traffic_delivery",
]
```

Use these exact strict models (all with `ConfigDict(extra="forbid")`):

```python
class McpLatencySample(BaseModel):
    kind: Literal["latency"] = "latency"
    version: Literal[1] = 1
    event_id: str = Field(min_length=8, max_length=128)
    timestamp_ms: int = Field(ge=0)
    request_id: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    edge_id: LatencyEdge
    metric_type: LatencyMetric
    duration_ms: int = Field(ge=0, le=86_400_000)
    status: Literal["ok", "error"] = "ok"

class McpFailureDiagnostic(BaseModel):
    kind: Literal["failure"] = "failure"
    version: Literal[1] = 1
    diagnostic_id: str = Field(min_length=8, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    client_id: str = Field(min_length=1, max_length=128)
    method: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, max_length=256)
    stage: FailureStage
    error_code: str = Field(min_length=1, max_length=64)
    http_status: int | None = Field(default=None, ge=100, le=599)
    retryable: bool | None = None
    started_at_ms: int | None = Field(default=None, ge=0)
    completed_at_ms: int = Field(ge=0)
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    request_bytes: int | None = Field(default=None, ge=0, le=100_000_000)
    response_bytes: int | None = Field(default=None, ge=0, le=100_000_000)
    summary: str = Field(min_length=1, max_length=500)

class McpGpuMetrics(BaseModel):
    index: int = Field(ge=0, le=64)
    name: str = Field(min_length=1, max_length=120)
    utilization_percent: float = Field(ge=0, le=100)
    memory_used_bytes: int = Field(ge=0)
    memory_total_bytes: int = Field(ge=0)
    temperature_c: float | None = Field(default=None, ge=-50, le=150)

class McpProcessMetrics(BaseModel):
    pid: int = Field(ge=0)
    cpu_percent: float | None = Field(default=None, ge=0)
    memory_percent: float | None = Field(default=None, ge=0, le=100)
    name: str = Field(min_length=1, max_length=160)

class McpBackendMetricsSample(BaseModel):
    kind: Literal["system"] = "system"
    version: Literal[1] = 1
    timestamp_ms: int = Field(ge=0)
    cpu_usage_percent: float | None = Field(default=None, ge=0, le=100)
    cpu_count: int = Field(ge=0, le=4096)
    load_avg: list[float] = Field(default_factory=list, max_length=3)
    memory_total_bytes: int | None = Field(default=None, ge=0)
    memory_available_bytes: int | None = Field(default=None, ge=0)
    disk_total_bytes: int | None = Field(default=None, ge=0)
    disk_used_bytes: int | None = Field(default=None, ge=0)
    disk_free_bytes: int | None = Field(default=None, ge=0)
    disk_read_bytes_per_s: float | None = Field(default=None, ge=0)
    disk_write_bytes_per_s: float | None = Field(default=None, ge=0)
    disk_read_ops_per_s: float | None = Field(default=None, ge=0)
    disk_write_ops_per_s: float | None = Field(default=None, ge=0)
    network_rx_bytes_per_s: float | None = Field(default=None, ge=0)
    network_tx_bytes_per_s: float | None = Field(default=None, ge=0)
    uptime_seconds: int | None = Field(default=None, ge=0)
    gpu_status: Literal["available", "unavailable", "error"] = "unavailable"
    gpus: list[McpGpuMetrics] = Field(default_factory=list, max_length=16)
    cptr_process: McpProcessMetrics | None = None
    processes: list[McpProcessMetrics] = Field(default_factory=list, max_length=10)

class McpDiagnosticsBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[McpLatencySample | McpFailureDiagnostic] = Field(min_length=1, max_length=100)
```

Before a failure is stored, run `redact_external_text(summary)` and then truncate to 500 characters. Tests must prove secrets and `/home`, `/tmp`, `/var`, `/opt`, `/srv`, `/private`, or Windows host paths do not survive the stored summary.

Tests must prove a window `[10, 20, 30, 40, 100]` produces:

```python
latest == 100
average == 40.0
p50 == 30
p95 == 100
max == 100
sample_count == 5
```

using nearest-rank percentile calculation. Also prove event dedupe, latency window truncation, failure ring truncation, subscriber overflow behavior, and rejection of extra fields such as `authorization`, `stack`, or `headers`.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest tests/test_mcp_diagnostics.py -q
```

Expected: missing module.

- [ ] **Step 3: Implement strict models and bounded store**

Use `ConfigDict(extra="forbid")` for every public Pydantic model. Default bounds:

```python
max_latency_samples_per_edge = 120
max_failures = 250
max_system_samples = 60
subscriber_queue_size = 64
```

Allow environment overrides only through bounded integer parsing consistent with the existing Traffic/Activity stores.

Compute aggregate `health` from the latest sample outcome plus p95. If the latest sample status is `error`, health is `error`; otherwise health is `degraded` when p95 meets/exceeds the configured threshold and `healthy` below it. Use these bounded environment settings and defaults:

```python
CPTR_MCP_DIAGNOSTICS_OBSERVED_DEGRADED_MS = 5000
CPTR_MCP_DIAGNOSTICS_HANDOFF_DEGRADED_MS = 100
CPTR_MCP_DIAGNOSTICS_BACKEND_RTT_DEGRADED_MS = 1000
```

Clamp each threshold to `1..86_400_000` ms. This keeps health policy server-side/configurable rather than hiding thresholds in the UI.

Snapshot shape:

```python
{
    "version": 1,
    "sequence": sequence,
    "latency": {
        edge_id: {
            "metric_type": metric_type,
            "latest_ms": latest,
            "average_ms": average,
            "p50_ms": p50,
            "p95_ms": p95,
            "max_ms": maximum,
            "sample_count": count,
            "last_updated_ms": timestamp,
            "latest_status": status,
            "health": health,
        }
    },
    "failures": failures,
    "system": system_samples,
    "stream_health": {
        "subscriber_count": subscriber_count,
        "slow_subscriber_drops": slow_subscriber_drops,
        "latency_sample_capacity_per_edge": max_latency_samples_per_edge,
        "failure_capacity": max_failures,
        "system_sample_capacity": max_system_samples,
        "subscriber_queue_capacity": subscriber_queue_size,
    },
}
```

- [ ] **Step 4: Add optional correlation to Traffic and Activity**

Add only:

```python
correlation_id: str | None = Field(default=None, max_length=128)
```

to both event contracts. Update tests to prove the field is accepted and that Traffic still contains none of `arguments_json`, `result_json`, `error_json`, `authorization`, or `headers`.

- [ ] **Step 5: Run focused tests and commit**

```bash
.venv/bin/python -m pytest tests/test_mcp_diagnostics.py tests/test_mcp_traffic.py tests/test_mcp_activity.py -q
ruff check cptr/services/mcp_diagnostics.py cptr/services/mcp_traffic.py cptr/services/mcp_activity.py tests/test_mcp_diagnostics.py
ruff format --check cptr/services/mcp_diagnostics.py cptr/services/mcp_traffic.py cptr/services/mcp_activity.py tests/test_mcp_diagnostics.py
git diff --check
git add cptr/services/mcp_diagnostics.py cptr/services/mcp_traffic.py cptr/services/mcp_activity.py tests/test_mcp_diagnostics.py tests/test_mcp_traffic.py tests/test_mcp_activity.py
git commit -m "feat: add bounded MCP diagnostics state"
```

---

### Task 3: Add the non-blocking live backend metrics sampler

**Files:**
- Create: `cptr/services/system_metrics.py`
- Create: `tests/test_system_metrics.py`

**Interfaces:**
- Produces this exact `BackendCounterSnapshot` dataclass for monotonic/current counters:

```python
@dataclass(frozen=True)
class BackendCounterSnapshot:
    timestamp_ms: int
    cpu_total: int | None
    cpu_idle: int | None
    cpu_count: int
    memory_total: int | None
    memory_available: int | None
    disk_total: int | None
    disk_used: int | None
    disk_free: int | None
    disk_read_bytes: int | None
    disk_write_bytes: int | None
    disk_read_ops: int | None
    disk_write_ops: int | None
    network_rx_bytes: int | None
    network_tx_bytes: int | None
    uptime_seconds: int | None
    load_avg: list[float]
    cptr_process_cpu_ticks: int | None
    cptr_process_rss_bytes: int | None
    clock_ticks_per_second: int | None
    cptr_process_name: str
    processes: list[McpProcessMetrics]
    gpus: list[McpGpuMetrics]
    gpu_status: Literal["available", "unavailable", "error"]
```

- Produces `collect_backend_counters() -> BackendCounterSnapshot` for blocking synchronous collection.
- Produces `derive_backend_metrics(previous, current) -> McpBackendMetricsSample`.
- Produces `BackendMetricsSampler(store, interval_seconds=1.0)` with async `ensure_started()`, async `sample_once()`, and async `close()`.
- Produces singleton `mcp_metrics_sampler = BackendMetricsSampler(mcp_diagnostics_store, interval_seconds=_bounded_env_int("CPTR_MCP_SYSTEM_METRICS_INTERVAL_MS", 1000, 500, 10_000) / 1000)`.
- Uses `asyncio.to_thread(collect_backend_counters)` so probes never block the event loop.

- [ ] **Step 1: Write deterministic RED tests for rate derivation**

Given two Linux-like counter snapshots one second apart:

```python
previous = BackendCounterSnapshot(
    timestamp_ms=1000,
    cpu_total=1000,
    cpu_idle=400,
    cpu_count=8,
    memory_total=1000,
    memory_available=400,
    disk_total=2000,
    disk_used=1000,
    disk_free=1000,
    disk_read_bytes=10_000,
    disk_write_bytes=20_000,
    disk_read_ops=100,
    disk_write_ops=200,
    network_rx_bytes=30_000,
    network_tx_bytes=40_000,
    uptime_seconds=10,
    load_avg=[0.5, 0.4, 0.3],
    cptr_process_cpu_ticks=100,
    cptr_process_rss_bytes=100,
    clock_ticks_per_second=100,
    cptr_process_name="cptr",
    processes=[],
    gpus=[],
    gpu_status="unavailable",
)
current = replace(
    previous,
    timestamp_ms=2000,
    cpu_total=1200,
    cpu_idle=450,
    disk_read_bytes=12_000,
    disk_write_bytes=25_000,
    disk_read_ops=110,
    disk_write_ops=220,
    network_rx_bytes=33_000,
    network_tx_bytes=44_000,
    cptr_process_cpu_ticks=150,
    cptr_process_rss_bytes=120,
)
```

Assert CPU usage is `75.0`, disk read/write rates are `2000/5000 B/s`, read/write ops are `10/20 ops/s`, network rates are `3000/4000 B/s`, and `cptr_process` reports PID `os.getpid()`, name `cptr`, CPU `50.0%`, and memory `12.0%` for this deterministic fixture.

- [ ] **Step 2: Add capability tests**

Patch `shutil.which("nvidia-smi")` to `None` and assert GPU status is `unavailable` with an empty GPU list. Patch the subprocess call to return a CSV row such as:

```text
0, NVIDIA RTX 2080 Ti, 42, 1024, 11264, 55
```

and assert parsed GPU utilization, memory, and temperature are bounded numeric values.

- [ ] **Step 3: Implement cross-platform collection without new dependencies**

On Linux read `/proc/stat`, `/proc/meminfo`, `/proc/uptime`, `/proc/diskstats`, `/proc/net/dev`, `/proc/self/stat`, `/proc/self/statm`, and `shutil.disk_usage(Path.home())`. Aggregate physical disk counters while excluding loop/ram devices. Use `os.getloadavg()` where available. Derive CPTR process CPU from the delta of `/proc/self/stat` user+system ticks and `SC_CLK_TCK`; derive its memory percentage from `/proc/self/statm` RSS versus total RAM. Collect the top process list with the existing bounded `ps -eo pid,pcpu,pmem,comm --sort=-pcpu --no-headers` pattern, maximum 10 rows.

On Darwin/Windows reuse platform-native basic CPU/RAM/disk information where practical; unsupported disk/network I/O counters remain `None`. Do not shell out repeatedly for a value available from `/proc` on Linux.

Probe NVIDIA only when `shutil.which("nvidia-smi")` succeeds, using a bounded timeout and query fields:

```text
index,name,utilization.gpu,memory.used,memory.total,temperature.gpu
```

- [ ] **Step 4: Implement the sampler loop**

`ensure_started()` is idempotent. The loop sleeps by awaiting an `asyncio.Event`/timeout, samples in a thread, derives rates from the previous counter snapshot, and calls `await store.record_system_sample(sample)`. All probe exceptions are caught and represented as unavailable/error capability state; they are not raised into request handlers.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_system_metrics.py tests/test_mcp_diagnostics.py -q
ruff check cptr/services/system_metrics.py tests/test_system_metrics.py
ruff format --check cptr/services/system_metrics.py tests/test_system_metrics.py
git diff --check
git add cptr/services/system_metrics.py tests/test_system_metrics.py
git commit -m "feat: stream bounded backend system metrics"
```

---

### Task 4: Add authenticated diagnostics ingestion/snapshot/SSE APIs

**Files:**
- Modify: `cptr/routers/mcp.py`
- Modify: `cptr/routers/gateway.py`
- Modify: `cptr/app.py`
- Create: `tests/test_mcp_diagnostics_api.py`
- Modify: `tests/test_mcp_activity_api.py`
- Modify: `tests/test_mcp_traffic_api.py`

**Interfaces:**
- Plugin writer endpoint: `POST /api/mcp/diagnostics/events`, scope `mcp:diagnostics:write`.
- Admin endpoints: `GET /api/mcp/diagnostics/snapshot`, `GET /api/mcp/diagnostics/stream`.
- Existing topology config endpoints from Task 1 remain admin-only.
- Diagnostics stream event names: `snapshot`, `latency`, `failure`, `system`; keepalive comments every 15 seconds.

- [ ] **Step 1: Write RED auth/API tests**

Prove:

```python
auth.assert_awaited_once()
self.assertEqual(auth.await_args.args[1], "mcp:diagnostics:write")
```

Missing scope maps to 403; invalid token maps to 401. Snapshot and stream call `require_admin`. The exact POST path bypasses cookie auth, while GET to the same ingestion path does not. `DEFAULT_CONTROL_SCOPES` contains the new scope exactly once.

- [ ] **Step 2: Add exact middleware and scope changes**

Extend the existing exact-path POST bypass set to include only:

```python
/api/mcp/traffic/events
/api/mcp/activity/events
/api/mcp/diagnostics/events
```

Do not bypass `/api/mcp/diagnostics/snapshot` or `/stream`.

- [ ] **Step 3: Add router writer and admin stream**

Add `_require_diagnostics_writer` following the existing Traffic/Activity error mapping. The stream subscribes only when iteration starts and unsubscribes in `finally`.

Before snapshot/stream output, call `await mcp_metrics_sampler.ensure_started()`; tests patch the sampler with an idempotent fake.

- [ ] **Step 4: Cross-channel privacy/correlation test**

Create one Traffic event, one Activity event, and one Diagnostic failure sharing `correlation_id="corr-1"`. Assert all three snapshots contain `corr-1`, Traffic does not contain Activity payload keys, and Diagnostics does not contain `arguments_json` or `result_json`.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_mcp_diagnostics_api.py tests/test_mcp_activity_api.py tests/test_mcp_traffic_api.py -q
ruff check cptr/routers/mcp.py cptr/routers/gateway.py cptr/app.py tests/test_mcp_diagnostics_api.py
ruff format --check cptr/routers/mcp.py cptr/routers/gateway.py cptr/app.py tests/test_mcp_diagnostics_api.py
git diff --check
git add cptr/routers/mcp.py cptr/routers/gateway.py cptr/app.py tests/test_mcp_diagnostics_api.py tests/test_mcp_activity_api.py tests/test_mcp_traffic_api.py
git commit -m "feat: expose MCP diagnostics APIs"
```

---

### Task 5: Instrument the ChatGPT plugin for fallback identity, correlation, controlled-hop latency, and safe failures

**Files:**
- Create: `server/mcp-diagnostics.ts`
- Modify: `server/mcp-traffic.ts`
- Modify: `server/mcp-activity.ts`
- Modify: `server/client/computer-client.ts`
- Modify: `server/index.ts`
- Modify: `server/mcp.ts`
- Create: `tests/mcp-diagnostics.test.ts`
- Modify: `tests/mcp-traffic.test.ts`
- Modify: `tests/mcp-activity.test.ts`
- Modify: `tests/mcp.test.ts`

**Interfaces:**
- `McpRequestContextValue` adds `correlationId: string` and mutable `outcome: { failed: boolean; errorCode: McpTrafficErrorCode | null }` so a caught/returned tool error can make the enclosing Traffic request terminal state `error` without changing the MCP response.
- Traffic/Activity output adds `correlation_id: string | null`.
- Produces `McpDiagnosticsEmitter` with `latency(input: Omit<McpLatencyDiagnostic, "kind"|"version"|"event_id"|"timestamp_ms">): void`, `failure(input: Omit<McpFailureDiagnostic, "kind"|"version"|"diagnostic_id"|"completed_at_ms">): void`, async `flush(): Promise<void>`, async `close(): Promise<void>`, and bounded queue semantics matching existing emitters.
- Extends `McpTrafficEmitterOptions` with `onDeliveryFailure?: (error: unknown, events: readonly McpTrafficEvent[]) => void` and `McpActivityEmitterOptions` with `onDeliveryFailure?: (error: unknown, events: readonly McpActivityEvent[]) => void`; callbacks are best-effort and exceptions from them are swallowed.
- `ComputerClient.ingestMcpDiagnostics(events: McpDiagnosticEvent[]): Promise<void>` POSTs `{events}` to `/api/mcp/diagnostics/events`.
- `ComputerClient.setRequestObserver(observer: ((observation: BackendRequestObservation) => void) | null): void` observes existing backend HTTP requests without changing their results.
- ChatGPT adapter fallback in `trafficClientFromRequest()` becomes `normalizeMcpClient({ name: "ChatGPT" })`; `normalizeMcpClient(undefined)` remains generic Unknown.

- [ ] **Step 1: Write RED identity/correlation tests**

Keep the existing generic assertion:

```ts
assert.equal(normalizeMcpClient(undefined).label, "Unknown MCP Client");
```

Add a source/runtime test for the ChatGPT adapter boundary proving its no-metadata fallback resolves to `{ id: "chatgpt", label: "ChatGPT" }`.

Add tests that one real SDK `tools/call` produces Traffic and Activity events sharing one non-empty `correlation_id` and request ID.

- [ ] **Step 2: Define strict diagnostics event types and queue**

Use discriminated event kinds:

```ts
type McpLatencyDiagnostic = {
  kind: "latency";
  version: 1;
  event_id: string;
  timestamp_ms: number;
  request_id: string | null;
  correlation_id: string | null;
  edge_id: "client-mcp-connector" | "mcp-connector-cptr-mcp" | "cptr-mcp-cptr-backend";
  metric_type: "observed_request_time" | "adapter_handoff" | "backend_api_rtt";
  duration_ms: number;
  status: "ok" | "error";
};

type McpFailureDiagnostic = {
  kind: "failure";
  version: 1;
  diagnostic_id: string;
  request_id: string | null;
  correlation_id: string | null;
  session_id: string | null;
  client_id: string;
  method: string | null;
  tool_name: string | null;
  stage: "client_transport" | "mcp_connector" | "cptr_mcp" | "cptr_backend" | "activity_delivery" | "traffic_delivery";
  error_code: string;
  http_status: number | null;
  retryable: boolean | null;
  started_at_ms: number | null;
  completed_at_ms: number;
  duration_ms: number | null;
  request_bytes: number | null;
  response_bytes: number | null;
  summary: string;
};

export type McpDiagnosticEvent = McpLatencyDiagnostic | McpFailureDiagnostic;
```

Bound identifiers to 128, method 128, tool 256, summary 500, duration to 86,400,000 ms, byte counts to 100,000,000, and HTTP status to 100–599 or null. Sanitize summaries before enqueueing.

`McpDiagnosticsEmitter` uses production defaults `batchSize=20`, `flushMs=250`, `maxQueue=500`, bounded by environment variables and explicit constructor overrides only for deterministic tests.

- [ ] **Step 3: Propagate opaque correlation IDs**

At the HTTP request boundary:

```ts
const requestId = randomUUID();
const correlationId = randomUUID();
const outcome = { failed: false, errorCode: null as McpTrafficErrorCode | null };
```

Store `requestId`, `correlationId`, and `outcome` in AsyncLocalStorage. Pass correlation into Traffic `requestStarted/requestFinished/requestFailed`, tool events, and Activity `started/complete/failed`.

- [ ] **Step 4: Measure truthful latency classes**

For each completed/failed request, emit `client-mcp-connector / observed_request_time` using the same total adapter-observed request duration already used for Traffic; never call it RTT.

Record `mcp-connector-cptr-mcp / adapter_handoff` as the bounded time spent in adapter setup immediately before entering the MCP SDK request handler.

Add a generic backend request observer around the existing `ComputerClient` fetch boundary:

```ts
type BackendRequestObservation = {
  method: string;
  path: string;
  status: number | null;
  durationMs: number;
  error: unknown | null;
};
```

The observer must never alter, await beyond the synchronous callback, or replace the actual response/error. In `server/index.ts`, combine it with `mcpRequestContext.getStore()` and emit `cptr-mcp-cptr-backend / backend_api_rtt`.

- [ ] **Step 5: Make all observable request/tool failures terminally visible without changing responses**

At backend HTTP failure, the `ComputerClient` observer emits stage `cptr_backend`, safe status/retryability from `ComputerApiError`, and a sanitized summary. The tool wrapper must not duplicate that as a second `cptr_mcp` root-cause record when the caught value is `ComputerApiError`; for non-backend handler exceptions it emits one `cptr_mcp` failure.

The existing wrapper catches exceptions and returns an MCP `{ isError: true }` response. Before returning that response it must set:

```ts
if (trafficContext) {
  trafficContext.outcome.failed = true;
  trafficContext.outcome.errorCode = normalizeTrafficErrorCode(error);
}
```

Also detect a handler that directly returns an MCP error result without throwing:

```ts
const record = value && typeof value === "object" && !Array.isArray(value)
  ? value as Record<string, unknown>
  : null;
const returnedToolError = record?.isError === true;
```

For `returnedToolError`, emit `tool_failed` + Activity `failed`, set `outcome.failed=true` / `errorCode="tool_error"`, and use the already bounded/redacted `terminalToolResult(value)` as Activity error payload. Do not emit a `tool_finished`/Activity `complete` pair for the same returned error.

Extend the existing response-byte tracker to capture at most 16,384 bytes of non-stream JSON response text solely long enough to detect a top-level JSON-RPC `error`. Expose:

```ts
type ResponseObservation = {
  bytes: () => number;
  statusCode: () => number;
  jsonRpcError: () => { code: string; message: string } | null;
  restore: () => void;
};
```

Never persist the captured body. Sanitize the JSON-RPC error message immediately and discard captured bytes after the request. After the SDK handler returns:

1. if `context.outcome.failed`, emit Traffic `request_failed` with its normalized code and do not emit `request_finished`;
2. else if HTTP status is `>=400` or `jsonRpcError()` is non-null, emit Traffic `request_failed` plus one `mcp_connector` Diagnostic failure;
3. else emit Traffic `request_finished`.

Authentication, oversized-body, malformed-JSON, or transport rejection that happens before a request context exists emits a bounded `client_transport` Diagnostic failure with null request/correlation IDs and the ChatGPT adapter fallback client ID. It must not include raw request body or headers.

Reuse the existing public error/path sanitization behavior; do not serialize arbitrary `Error.stack`, headers, response bodies, or request arguments.

- [ ] **Step 6: Add diagnostics delivery plus Traffic/Activity delivery-failure visibility**

`ComputerClient.ingestMcpDiagnostics()` uses the existing base URL/token and throws only a generic sanitized delivery error. The Diagnostics emitter swallows delivery rejection and increments dropped count exactly like Traffic/Activity.

Instantiate `mcpDiagnostics` before the Traffic/Activity emitters. In each existing emitter's `drain()` catch, preserve the current dropped-count behavior and invoke its optional `onDeliveryFailure` callback inside a nested `try/catch`. Wire those callbacks in `server/index.ts` to emit one bounded Diagnostic failure per rejected batch:

```ts
stage: "traffic_delivery" | "activity_delivery"
error_code: "telemetry_delivery_failed"
http_status: null
retryable: true
summary: "MCP traffic delivery failed." | "MCP activity delivery failed."
```

Use the first event's request/session/correlation/client metadata only when present; otherwise use null IDs and client `chatgpt` at this ChatGPT adapter boundary. Never include the rejected response body, bearer token, or raw delivery exception message. A diagnostics-delivery rejection itself is counted/dropped locally and must not recursively emit another diagnostic.

Tests must prove Traffic/Activity delivery callbacks cannot throw through `flush()` or a real tool call and that a functioning Diagnostics channel receives the correct `traffic_delivery`/`activity_delivery` stage.

- [ ] **Step 7: Verify plugin contract and commit**

```bash
npm test
npm run typecheck
npm run build
npm run check:mcp-live-activity
git diff --check
git add server tests scripts package.json
git commit -m "feat: emit correlated MCP diagnostics"
```

Expected: visible MCP action count/names remain unchanged by this feature.

---

### Task 6: Add typed frontend diagnostics/config stores and SSE clients

**Files:**
- Modify: `cptr/frontend/src/lib/apis/mcp.ts`
- Create: `cptr/frontend/src/lib/stores/mcp-diagnostics.ts`
- Create: `cptr/frontend/src/lib/stores/mcp-topology.ts`
- Modify: `cptr/frontend/src/lib/stores/mcp-traffic.ts`
- Modify: `cptr/frontend/src/lib/stores/mcp-activity.ts`
- Modify: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

**Interfaces:**
- `getMcpTopologyConfig()`, `updateMcpTopologyConfig(aliases)`.
- `getMcpDiagnosticsSnapshot()`, `openMcpDiagnosticsStream(callbacks)`.
- `McpTopologySelection = { kind: 'client'|'node'|'edge'; id: string } | null`.
- `displayTopologyLabel(id, canonical, aliases) -> string`.
- Diagnostics reducer hydrates/appends bounded latency/failure/system state and ignores stale sequences.
- Request/Activity rows expose `correlationId`.

- [ ] **Step 1: Add RED frontend reducer/API tests**

Assert API source has authenticated cookie-based EventSource:

```js
assert.match(api, /new EventSource\(['"]\/api\/mcp\/diagnostics\/stream['"]\)/);
assert.doesNotMatch(api, /diagnostics\/stream[^\n]*(token|authorization|bearer)/i);
```

Test alias projection:

```js
assert.equal(displayTopologyLabel('cptr-backend', 'CPTR Backend', {'cptr-backend': 'Workstation'}), 'Workstation');
assert.equal(displayTopologyLabel('cptr-backend', 'CPTR Backend', {}), 'CPTR Backend');
```

Test reducer ignores duplicate/stale sequence, keeps failure/system arrays bounded, and replaces latest per-edge aggregate.

- [ ] **Step 2: Implement exact TypeScript contracts matching Python JSON**

Do not duplicate snake/camel translation inconsistently. API interfaces mirror wire JSON in snake_case; reducer/UI state converts once to camelCase.

- [ ] **Step 3: Carry correlation through existing reducers**

Add `correlationId: event.correlation_id` to recent request rows and Activity rows. This field is metadata only and must not alter row identity or dedupe behavior.

- [ ] **Step 4: Verify and commit**

```bash
cd cptr/frontend
node --test tests/mcp-traffic-topology.test.mjs
npx prettier --check src/lib/apis/mcp.ts src/lib/stores/mcp-diagnostics.ts src/lib/stores/mcp-topology.ts src/lib/stores/mcp-traffic.ts src/lib/stores/mcp-activity.ts tests/mcp-traffic-topology.test.mjs
cd ../..
git diff --check
git add cptr/frontend/src/lib/apis/mcp.ts cptr/frontend/src/lib/stores cptr/frontend/tests/mcp-traffic-topology.test.mjs
git commit -m "feat: add MCP diagnostics frontend state"
```

---

### Task 7: Make every topology node/edge selectable and every node renameable

**Files:**
- Modify: `cptr/frontend/src/lib/components/mcp/McpTopology.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpTopologyGraph.svelte`
- Create: `cptr/frontend/src/lib/components/mcp/McpTopologyDetail.svelte`
- Modify: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

**Interfaces:**
- `McpTopologyGraph` receives aliases, latency summaries, failure health, selected topology selection, and `onselect(selection)`.
- Fixed node IDs are `mcp-connector`, `cptr-mcp`, `cptr-backend`; dynamic client IDs remain telemetry IDs.
- All node detail views use the same `McpTopologyDetail` alias editor.

- [ ] **Step 1: Write RED source/behavior contracts**

Assert fixed infrastructure nodes have `role="button"`, `tabindex="0"`, Enter/Space handlers, and stable IDs. Assert edge selection exists and edge accessible labels include the metric type, not just a number.

Assert alias save uses `updateMcpTopologyConfig` and reset sends `{[nodeId]: null}`.

- [ ] **Step 2: Load config and diagnostics alongside Traffic**

On mount/reconnect, fetch Traffic snapshot, Diagnostics snapshot, and topology config independently. A diagnostics/config failure must not suppress the Traffic topology; show unavailable diagnostics while continuing Traffic reconnect behavior.

- [ ] **Step 3: Render aliases without changing IDs**

Dynamic client node text:

```ts
displayTopologyLabel(node.id, node.label, aliases)
```

Fixed node text uses the same function and fixed canonical names. Selection/correlation continues to use canonical IDs.

- [ ] **Step 4: Add shared alias editor**

`McpTopologyDetail.svelte` displays canonical ID, canonical name, current alias, a bounded text input, Save, and Reset to default. Disable Save while submitting; display API validation errors without discarding the typed alias.

- [ ] **Step 5: Add latency badges to edges**

Render latest value only when a sample exists. Detail copy must distinguish:

```text
Observed request time
Adapter handoff
Backend API RTT
```

Missing metrics render `—`/`Unavailable`. Health/error state derives from diagnostics status, never from arbitrary client-side thresholds.

- [ ] **Step 6: Verify and commit**

```bash
cd cptr/frontend
node --test tests/mcp-traffic-topology.test.mjs
npx prettier --check src/lib/components/mcp/McpTopology.svelte src/lib/components/mcp/McpTopologyGraph.svelte src/lib/components/mcp/McpTopologyDetail.svelte tests/mcp-traffic-topology.test.mjs
cd ../..
git diff --check
git add cptr/frontend/src/lib/components/mcp/McpTopology.svelte cptr/frontend/src/lib/components/mcp/McpTopologyGraph.svelte cptr/frontend/src/lib/components/mcp/McpTopologyDetail.svelte cptr/frontend/tests/mcp-traffic-topology.test.mjs
git commit -m "feat: add editable MCP topology details"
```

---

### Task 8: Add the live CPTR Backend system monitor

**Files:**
- Create: `cptr/frontend/src/lib/components/mcp/McpBackendMonitor.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpTopologyDetail.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpTopology.svelte`
- Modify: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

**Interfaces:**
- `McpBackendMonitor` consumes the latest bounded system sample plus short history.
- It renders available metrics and explicit unavailable states without triggering new server probes itself.

- [ ] **Step 1: Add RED rendering contracts**

Require visible labels for:

```text
CPU
RAM
Disk
Disk read
Disk write
Disk IOPS
Network RX
Network TX
GPU
GPU memory
GPU temperature
Uptime
Processes
Telemetry health
```

and an explicit `Unavailable` path for missing GPU/I/O values.

- [ ] **Step 2: Implement tokenized metric cards**

Use `app-surface`, `app-subtle-surface`, `app-muted`, `--app-accent`, and existing semantic status colors. Do not hard-code a new dark-only palette.

Show percentage bars only for bounded percentage values. Format byte throughput as `B/s`, `KB/s`, `MB/s`; IOPS as `ops/s`. GPU can render multiple devices by stable GPU index.

- [ ] **Step 3: Add bounded sparklines without a chart dependency**

Use small inline SVG polylines derived from the already-bounded ~60-sample history for CPU, RAM %, disk throughput, and network throughput. Do not add a chart package or preserve additional browser history beyond the server snapshot.

- [ ] **Step 4: Render only for `cptr-backend` selection**

The backend detail includes backend API latency, system monitor, recent backend failures, and Traffic/Activity/Diagnostics subscriber/drop counters. Other node types do not render host metrics.

- [ ] **Step 5: Verify and commit**

```bash
cd cptr/frontend
node --test tests/mcp-traffic-topology.test.mjs
npx prettier --check src/lib/components/mcp/McpBackendMonitor.svelte src/lib/components/mcp/McpTopologyDetail.svelte src/lib/components/mcp/McpTopology.svelte tests/mcp-traffic-topology.test.mjs
cd ../..
git diff --check
git add cptr/frontend/src/lib/components/mcp/McpBackendMonitor.svelte cptr/frontend/src/lib/components/mcp/McpTopologyDetail.svelte cptr/frontend/src/lib/components/mcp/McpTopology.svelte cptr/frontend/tests/mcp-traffic-topology.test.mjs
git commit -m "feat: show live backend metrics in MCP topology"
```

---

### Task 9: Surface structured request failures and link them to Activity

**Files:**
- Create: `cptr/frontend/src/lib/components/mcp/McpDiagnosticDetail.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpRecentRequests.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpTopology.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpActivityFeed.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpConsole.svelte`
- Modify: `cptr/frontend/src/routes/mcp/+page.svelte`
- Modify: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

**Interfaces:**
- Recent request rows match diagnostics by `correlationId` first, then request ID when correlation is absent.
- `McpTopology` emits `onrevealactivity(requestId, correlationId)`.
- `+page.svelte` switches to Console and passes a focused request into `McpConsole`/`McpActivityFeed`.
- Activity focus scrolls/highlights the matching row without deleting or filtering server history.

- [ ] **Step 1: Add RED error drill-down tests**

Require error rows to show the normalized stage, e.g. `cptr_backend`, and a detail component that renders:

```text
Stage
Error code
HTTP status
Retryable
Duration
Request ID
Correlation ID
Summary
```

Assert source does not render `stack`, `headers`, `authorization`, `cookie`, or raw request/response payloads from Diagnostics.

- [ ] **Step 2: Implement safe diagnostic matching**

Build a derived map keyed by correlation ID/request ID from the bounded diagnostics store. A request with no matching diagnostic keeps the existing normalized Traffic error code and shows `No deeper diagnostic was captured` rather than inventing a stage/root cause.

- [ ] **Step 3: Add Activity reveal flow**

When a diagnostic has matching Activity correlation, render `Show Activity`. Route state switches the top-level page view to Console, sets mobile Console view to Activity, and asks the Activity feed to scroll to/highlight the matching record.

- [ ] **Step 4: Preserve mobile compactness**

The mobile Recent Requests two-line summary remains horizontally scroll-free. Error stage can appear in the detail sheet/card rather than widening the compact row.

- [ ] **Step 5: Verify and commit**

```bash
cd cptr/frontend
node --test tests/*.mjs
npx prettier --check src/lib/components/mcp src/routes/mcp/+page.svelte tests/mcp-traffic-topology.test.mjs
npm run build:clean
cd ../..
git diff --check
git add cptr/frontend/src/lib/components/mcp cptr/frontend/src/routes/mcp/+page.svelte cptr/frontend/tests/mcp-traffic-topology.test.mjs
git commit -m "feat: add correlated MCP error diagnostics"
```

---

### Task 10: Extend real MCP acceptance to Traffic + Activity + Diagnostics

**Files:**
- Modify: `chatgpt-computer-plugin/scripts/check-mcp-live-activity-integration.mjs`
- Modify: `chatgpt-computer-plugin/package.json` only if adding `check:mcp-diagnostics-acceptance` improves clarity.
- Computer test fixture may reuse existing MCP stores/router fakes; do not add production-only test endpoints.

**Interfaces:**
- One real MCP SDK session proves all three channels correlate without privacy crossover.

- [ ] **Step 1: Extend the acceptance harness to capture Diagnostics POSTs**

The fake CPTR boundary accepts:

```text
/api/mcp/traffic/events
/api/mcp/activity/events
/api/mcp/diagnostics/events
```

and validates the same bearer token without logging it.

- [ ] **Step 2: Run one successful real SDK tool call**

Use a client named `ChatGPT` and `cptr_list_workspaces`. Assert:

- Traffic includes request/tool lifecycle but no arguments/results;
- Activity includes bounded/redacted started/complete input/output;
- Diagnostics contains controlled-hop latency samples;
- all applicable events share one request ID and one correlation ID;
- no diagnostic is mislabeled as ChatGPT internet RTT.

- [ ] **Step 3: Run a safe induced failure**

Have the fake backend return a structured non-secret error such as status 503 with `{code:"fixture_unavailable", message:"Fixture backend unavailable", retriable:true}`. Assert the MCP result follows the existing tool-error behavior and Diagnostics records stage `cptr_backend`, status 503, retryable true, sanitized summary, request ID, and correlation ID.

- [ ] **Step 4: Verify delivery-failure isolation**

Reject Diagnostics ingestion while allowing the underlying backend tool request to succeed. Assert the tool result is unchanged and the diagnostics emitter increments dropped events rather than throwing through the MCP call.

- [ ] **Step 5: Run complete plugin gates and commit**

```bash
npm test
npm run typecheck
npm run build
npm run check:mcp-live-activity
git diff --check
git add scripts package.json
git commit -m "test: verify MCP diagnostics end to end"
```

---

### Task 11: Run the complete Computer integration gate and browser verification

**Files:**
- No production changes expected unless verification finds a real defect.
- Screenshots may be stored under ignored `.cptr/screenshots/` only.

**Interfaces:**
- Produces an exact verified Computer feature commit suitable for release integration.

- [ ] **Step 1: Run backend MCP/diagnostics suites**

```bash
.venv/bin/python -m pytest \
  tests/test_mcp_traffic.py \
  tests/test_mcp_traffic_api.py \
  tests/test_mcp_activity.py \
  tests/test_mcp_activity_api.py \
  tests/test_mcp_topology_config.py \
  tests/test_mcp_diagnostics.py \
  tests/test_mcp_diagnostics_api.py \
  tests/test_system_metrics.py -q
```

Then run Ruff/format on every changed Python file and `git diff --check`.

- [ ] **Step 2: Run the entire frontend regression/build gate**

```bash
cd cptr/frontend
node --test tests/*.mjs
npx prettier --check src tests scripts package.json
npm run build:clean
cd ../..
git diff --check
```

Expected: all tests pass and the production warning gate is silent for every forbidden signature.

- [ ] **Step 3: Launch a disposable authenticated local fixture using the production build**

Seed real-shaped snapshot/SSE data with:

- ChatGPT client;
- aliases for at least one client and one infrastructure node;
- latency on all three edges;
- live CPU/RAM/disk/I/O/network sample;
- GPU available or explicit unavailable state;
- one successful request;
- one correlated safe failure with matching Activity.

The fixture must not persist credentials or modify product source.

- [ ] **Step 4: Verify desktop managed Chrome**

Confirm:

- no `Unknown MCP Client` for the ChatGPT fixture;
- every node opens detail and can rename/reset through the fixture API;
- edge labels use correct metric names;
- backend click opens system monitor;
- error request shows stage/summary and can reveal matching Activity;
- NightOwl token surfaces remain coherent.

Capture topology, backend monitor, and error detail screenshots.

- [ ] **Step 5: Verify 390×844 mobile**

Resize managed Chrome to exactly `390×844`. Confirm no horizontal overflow, compact Recent Requests remains two lines, node/edge detail is usable, alias controls have touch-friendly targets, backend metrics remain readable, and Activity reveal switches to the mobile Activity pane.

- [ ] **Step 6: Stop fixture/browser and confirm clean tree**

Remove temporary runtime/symlink artifacts, close managed Chrome, and run:

```bash
git status --short
git diff --check
```

Only ignored screenshots may remain.

---

### Task 12: Rebase/merge onto latest remote main, rerun exact release gates, and push `main`

**Files:**
- No new product files unless resolving genuine upstream feature conflicts.

**Interfaces:**
- Produces verified remote `main` commits for `computer` and `chatgpt-computer-plugin`.
- Does not modify the dirty primary `computer/main` worktree.

- [ ] **Step 1: Fetch current remote main for both repositories**

Network is explicitly authorized by the user's push request.

```bash
git fetch origin main
```

Run in both repository roots. Record `origin/main` SHA before integration.

- [ ] **Step 2: Create clean release worktrees from `origin/main`**

Do not checkout/reset the dirty primary `main` worktree. Instead create temporary release branches in new sibling worktrees:

```bash
git worktree add ../computer-release-main -b release/mcp-topology-diagnostics origin/main
```

and in the plugin repository:

```bash
git worktree add ../plugin-release-main -b release/mcp-topology-diagnostics origin/main
```

If a release branch name already exists, delete/recreate only that temporary release branch after confirming it is not an unmerged user branch.

Immediately run the **Execution harness preflight** from this plan in each fresh release worktree so the ignored `.venv`/`node_modules` runtimes are available without an install. Record whether each symlink was created so Step 6 removes only harness-created links.

- [ ] **Step 3: Merge the verified feature histories into the clean release branches**

Computer release worktree:

```bash
git merge --no-ff fix/mcp-mobile-runtime -m "feat: add MCP topology diagnostics"
```

Plugin release worktree:

```bash
git merge --no-ff feature/mcp-live-activity -m "feat: add MCP live activity and diagnostics"
```

Resolve only genuine conflicts introduced by upstream `main`. Never use `ours`/`theirs` wholesale for touched observability/auth/frontend files.

- [ ] **Step 4: Rerun the full release gates on the exact merge commits**

Computer release worktree:

```bash
.venv/bin/python -m pytest \
  tests/test_mcp_traffic.py tests/test_mcp_traffic_api.py \
  tests/test_mcp_activity.py tests/test_mcp_activity_api.py \
  tests/test_mcp_topology_config.py tests/test_mcp_diagnostics.py \
  tests/test_mcp_diagnostics_api.py tests/test_system_metrics.py -q
cd cptr/frontend
node --test tests/*.mjs
npm run build:clean
npx prettier --check src tests scripts package.json
cd ../..
git diff --check
```

Plugin release worktree:

```bash
npm test
npm run typecheck
npm run build
npm run check:mcp-live-activity
git diff --check
```

If upstream main changes the MCP contract intentionally, verify the exact current contract rather than forcing the historical count; diagnostics changes themselves must not be the cause of any tool-contract delta.

- [ ] **Step 5: Push each exact verified release HEAD to remote main**

First Computer:

```bash
git push origin HEAD:main
```

Then plugin:

```bash
git push origin HEAD:main
```

Do not force-push. If remote main moves between fetch and push, stop, fetch the new head, merge/rebase into the release worktree, rerun the relevant full gate, then retry a normal push.

- [ ] **Step 6: Verify remote main SHAs and preserve local dirty work**

First remove only test-harness symlinks that this plan created. In the Computer release worktree:

```bash
test ! -L .venv || unlink .venv
test ! -L cptr/frontend/node_modules || unlink cptr/frontend/node_modules
```

In the plugin release worktree:

```bash
test ! -L node_modules || unlink node_modules
```

Then run in each release worktree:

```bash
git fetch origin main
git rev-parse HEAD
git rev-parse origin/main
git status --short
```

Expected in each release worktree: `HEAD == origin/main` and clean status.

Finally re-check the original primary Computer checkout with `git status --short` and verify its unrelated dirty files are still present and unchanged. Remove only the temporary clean release worktrees after remote verification; do not delete the feature branches until the user asks.

---

## Final acceptance checklist

- [ ] ChatGPT-facing unidentified requests render as `ChatGPT`; generic unknown normalization remains truthful elsewhere.
- [ ] Every topology node can Save/Reset a server-persistent alias without changing its canonical ID.
- [ ] CPTR Backend selection displays bounded live CPU, RAM, disk, disk I/O, network, uptime, processes, telemetry health, and GPU available/unavailable state.
- [ ] Every edge displays truthful measured/observed timing with latest/average/p50/p95/max in detail.
- [ ] No client-facing value is falsely called internet RTT/ping.
- [ ] Failed requests show safe stage, code, status, retryability, duration, request/correlation IDs, and sanitized summary.
- [ ] Matching Activity can be revealed by correlation/request ID.
- [ ] Traffic stays metadata-only; Activity stays bounded/redacted; Diagnostics stays allowlist-only.
- [ ] Diagnostics/system probe/delivery failures cannot change primary MCP tool results.
- [ ] Backend, frontend, plugin, integration, production build, formatting, and diff-hygiene gates pass.
- [ ] Desktop and exact 390×844 mobile browser acceptance passes.
- [ ] Frontend `npm run build:clean` passes with zero forbidden warning signatures.
- [ ] Final candidate worktrees are clean.
- [ ] Remote Computer `main` and plugin `main` equal the exact verified release heads.
- [ ] Dirty unrelated primary Computer checkout remains untouched.
