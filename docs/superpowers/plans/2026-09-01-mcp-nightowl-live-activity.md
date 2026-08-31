# MCP NightOwl + Live Activity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish `/mcp` so it follows CPTR appearance tokens, works cleanly on mobile, shows the real client → MCP Connector → CPTR MCP → CPTR Backend path, and renders authoritative bounded/redacted ChatGPT MCP tool input/output in a live Console Activity feed.

**Architecture:** Keep the existing `/api/mcp/traffic/*` topology path metadata-only. Add a separate bounded MCP Activity producer/store/API/SSE path: the ChatGPT-facing plugin emits already-redacted tool lifecycle records from its existing one-time `registerTool()` wrapper through `ComputerClient`; CPTR ingests them into an in-memory `McpActivityStore`; the `/mcp` Console hydrates from snapshot + SSE while preserving downstream-server manual invocation as a separately labeled local source. The UI uses CPTR `--app-*` appearance tokens rather than page-specific white/gray palettes.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, asyncio, Svelte 5, TypeScript, Node test runner, `@modelcontextprotocol/sdk`, SSE, existing CPTR bearer/admin authentication, existing plugin terminal redaction helpers.

**Spec:** `docs/superpowers/specs/2026-09-01-mcp-nightowl-live-activity-design.md`

## Global Constraints

- Topology traffic remains metadata-only: `GET/POST /api/mcp/traffic/*` must never gain raw tool arguments/results.
- New activity payloads use a separate `/api/mcp/activity/*` namespace and dedicated `mcp:activity:write` bearer scope.
- Activity delivery is best-effort and must never fail or materially delay the primary MCP tool call.
- Reuse the plugin's existing `terminalJson`, `terminalToolResult`, error-envelope, and terminal redaction behavior before activity delivery.
- No chain-of-thought, authorization headers, cookies, bearer tokens, API keys, raw HTTP headers, unbounded binary/base64, or unbounded stdout/stderr may enter the Activity channel.
- Preserve the ChatGPT-visible MCP tool count, names, schemas, annotations, and behavior.
- V1 activity history is bounded and in-memory only; no database or external telemetry service.
- All mutable stores and subscriber queues are bounded.
- `/mcp` must derive visual surfaces from `--app-*` appearance tokens and therefore support NightOwl, light, system, and custom appearance settings.
- Mobile Recent Requests must not use the desktop `min-w-[39rem]` table layout.
- Mobile Console remains `Servers | Activity | Tool`, one full-width pane at a time.
- Back navigates to `/` with a minimum 44 px mobile target.
- Topology presentation path is `dynamic client → MCP Connector → CPTR MCP → CPTR Backend`.
- `Unknown MCP Client` remains only a dynamic client identity fallback.
- Existing dirty development checkouts must not be overwritten or reset.
- The current Computer branch is `fix/mcp-mobile-runtime`; its existing uncommitted mobile Console repair must be preserved and committed before worker creation so the source repository becomes clean.
- The plugin implementation branch will be `feature/mcp-live-activity`, created from current `chatgpt-computer-plugin/origin/main` at execution time.
- Do not push either repository unless the user explicitly asks for push/PR/deployment for this new repair.

---

## File map

### `computer`

- Preserve/modify `cptr/frontend/src/lib/components/mcp/McpConsole.svelte` — existing mobile pane repair plus unified authoritative Activity feed and local manual invocation presentation.
- Modify `cptr/frontend/tests/mcp-traffic-topology.test.mjs` — mobile Console, theme, Back, compact Recent Requests, topology infrastructure, Activity UI contracts.
- Create `cptr/services/mcp_activity.py` — strict activity schema, bounded ring/dedupe/subscribers, snapshot state.
- Create `tests/test_mcp_activity.py` — schema/bounds/dedupe/subscriber behavior.
- Modify `cptr/routers/mcp.py` — activity ingestion/snapshot/SSE endpoints and auth helper.
- Modify `cptr/routers/gateway.py` — add `mcp:activity:write` to default plugin control scopes.
- Modify `cptr/app.py` — exact POST-only auth-middleware pass-through for `/api/mcp/activity/events`, matching the existing traffic ingestion pattern.
- Create `tests/test_mcp_activity_api.py` — activity API authentication, middleware, snapshot/SSE behavior, traffic/activity separation.
- Modify `cptr/frontend/src/lib/apis/mcp.ts` — activity types + snapshot/SSE helpers.
- Create `cptr/frontend/src/lib/stores/mcp-activity.ts` — pure bounded activity reducer, phase folding, local/manual record merge.
- Create `cptr/frontend/src/lib/components/mcp/McpActivityFeed.svelte` — live activity rendering, reconnect state, clear-presentation behavior.
- Modify `cptr/frontend/src/lib/components/mcp/McpCallCard.svelte` — reusable tokenized bounded Input/Output/Error presentation for live and local activity.
- Modify `cptr/frontend/src/lib/components/mcp/McpServerList.svelte` — tokenized surfaces and verified refresh/reconnect/tool-list controls.
- Modify `cptr/frontend/src/lib/components/mcp/McpToolForm.svelte` — tokenized form controls and verified validation/invocation behavior.
- Modify `cptr/frontend/src/lib/components/mcp/McpTopology.svelte` — tokenized summary/layout shell.
- Modify `cptr/frontend/src/lib/components/mcp/McpTopologyGraph.svelte` — fixed infrastructure nodes and full request-path animation.
- Modify `cptr/frontend/src/lib/components/mcp/McpRecentRequests.svelte` — compact mobile list + desktop table.
- Modify `cptr/frontend/src/routes/mcp/+page.svelte` — Back control and tokenized route/header/tabs.

### `chatgpt-computer-plugin`

- Create `server/mcp-activity.ts` — strict activity types, bounded copy helpers, asynchronous queue/batcher, request/client correlation.
- Modify `server/client/computer-client.ts` — `ingestMcpActivity(events)` to `/api/mcp/activity/events` using existing CPTR URL/token.
- Modify `server/index.ts` — instantiate/close the activity emitter and pass it to every `createMcpServer()` instance.
- Modify `server/mcp.ts` — emit activity `started/complete/failed` from the existing one-time registered-tool wrapper, using existing redacted argument/result/error JSON.
- Create `tests/mcp-activity.test.ts` — queue, bounds, copy/redaction contract, ComputerClient delivery, failure isolation.
- Modify `tests/mcp.test.ts` and/or existing activity contract tests — every visible action gets normalized Activity and visible tool contract remains unchanged.
- Extend `scripts/check-mcp-traffic-integration.mjs` or create `scripts/check-mcp-live-activity-integration.mjs` — real SDK session verifies topology metadata plus Activity started/complete output records.
- Modify `package.json` only if a named check script is needed for the new integration verifier.

---

### Task 0: Lock the existing mobile Console repair and clean the Computer source branch

**Files:**
- Existing modified: `cptr/frontend/src/lib/components/mcp/McpConsole.svelte`
- Existing modified: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

**Interfaces:**
- Produces a clean `fix/mcp-mobile-runtime` source branch that includes the already-live one-pane mobile Console behavior.
- No new Activity API or theme changes are introduced in this task.

- [ ] **Step 1: Re-read the two existing modified files and inspect the exact diff**

Run:

```bash
git diff -- cptr/frontend/src/lib/components/mcp/McpConsole.svelte cptr/frontend/tests/mcp-traffic-topology.test.mjs
```

Verify the diff is limited to the approved mobile `Servers | Activity | Tool` one-pane behavior and its regression test.

- [ ] **Step 2: Run the existing focused MCP frontend regression**

Run:

```bash
node --test cptr/frontend/tests/mcp-traffic-topology.test.mjs
```

Expected: all current MCP topology/mobile tests pass.

- [ ] **Step 3: Run formatter and diff hygiene on the two files**

Run:

```bash
cd cptr/frontend
npx prettier --check src/lib/components/mcp/McpConsole.svelte tests/mcp-traffic-topology.test.mjs
cd ../..
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 4: Commit only the existing mobile repair**

```bash
git add cptr/frontend/src/lib/components/mcp/McpConsole.svelte cptr/frontend/tests/mcp-traffic-topology.test.mjs
git commit -m "fix: make MCP console usable on mobile"
```

- [ ] **Step 5: Confirm source branch is clean before Direct Coding Workers are created**

Run:

```bash
git status --short
```

Expected: no output.

---

### Task 1: Add the bounded CPTR MCP Activity schema/store

**Files:**
- Create: `cptr/services/mcp_activity.py`
- Create: `tests/test_mcp_activity.py`

**Interfaces:**
- Produces `McpActivityClient`, `McpActivityEvent`, `McpActivityBatch`, `McpActivityStore`, and singleton `mcp_activity_store`.
- `await McpActivityStore.ingest(events: list[McpActivityEvent]) -> dict[str, int]`
- `await McpActivityStore.snapshot() -> dict[str, object]`
- `McpActivityStore.subscribe() -> asyncio.Queue[dict[str, object]]`
- `McpActivityStore.unsubscribe(queue) -> None`

- [ ] **Step 1: Write strict schema and store tests first**

Use a helper with concrete valid events:

```python
from cptr.services.mcp_activity import McpActivityEvent, McpActivityStore

BASE_TS = 1_788_000_000_000


def activity_event(event_id: str, phase: str = "started") -> McpActivityEvent:
    return McpActivityEvent(
        version=1,
        event_id=event_id,
        sequence=1,
        timestamp_ms=BASE_TS,
        client={"id": "chatgpt", "label": "ChatGPT", "version": "1.0"},
        session_id="session-1",
        request_id="request-1",
        tool_name="cptr_list_workspaces",
        title="List workspaces",
        phase=phase,
        summary="Working: List workspaces.",
        arguments_json='{"include_unavailable":false}' if phase == "started" else None,
        result_json='{"workspaces":[]}' if phase == "complete" else None,
        error_json='{"code":"mcp_tool_error"}' if phase == "failed" else None,
        duration_ms=12 if phase != "started" else None,
    )
```

Tests must prove:

- `extra="forbid"` rejects an `authorization` field;
- `tool_name` max length is 256;
- `title` max length is 160;
- `summary` max length is 500;
- each JSON payload max length is 13_000 characters;
- event ring truncates to `max_events`;
- duplicate `event_id` is ignored;
- subscriber queues are bounded and non-blocking;
- `snapshot()` reports version, sequence, event capacity, subscriber count, and newest-last events.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_mcp_activity.py -q
```

Expected: import failure because `cptr.services.mcp_activity` does not exist.

- [ ] **Step 3: Implement the minimal strict schema and bounded store**

Use these exact public models:

```python
class McpActivityClient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=80)
    version: str | None = Field(default=None, max_length=64)


class McpActivityEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    event_id: str = Field(min_length=8, max_length=128)
    sequence: int = Field(ge=1)
    timestamp_ms: int = Field(ge=0)
    client: McpActivityClient
    session_id: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)
    tool_name: str = Field(min_length=1, max_length=256)
    title: str | None = Field(default=None, max_length=160)
    phase: Literal["started", "complete", "failed"]
    summary: str = Field(min_length=1, max_length=500)
    arguments_json: str | None = Field(default=None, max_length=13_000)
    result_json: str | None = Field(default=None, max_length=13_000)
    error_json: str | None = Field(default=None, max_length=13_000)
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
```

`McpActivityBatch.events` must be `Field(min_length=1, max_length=100)`.

The store defaults must be environment-driven with bounded fallbacks:

```python
mcp_activity_store = McpActivityStore(
    max_events=_bounded_env_int("CPTR_MCP_ACTIVITY_MAX_EVENTS", 250, 25, 2000),
    subscriber_queue_size=_bounded_env_int("CPTR_MCP_ACTIVITY_SUBSCRIBER_QUEUE", 64, 8, 512),
)
```

Use one `asyncio.Lock`, `deque(maxlen=max_events)`, bounded dedupe deque/set, and `put_nowait` subscriber fan-out identical in failure semantics to `McpTrafficStore`.

- [ ] **Step 4: Run Task 1 tests and verify GREEN**

```bash
.venv/bin/python -m pytest tests/test_mcp_activity.py -q
```

Expected: all Task 1 tests pass.

- [ ] **Step 5: Run Ruff/format on Task 1 files**

```bash
.venv/bin/python -m ruff check cptr/services/mcp_activity.py tests/test_mcp_activity.py
.venv/bin/python -m ruff format --check cptr/services/mcp_activity.py tests/test_mcp_activity.py
git diff --check
```

- [ ] **Step 6: Commit Task 1**

```bash
git add cptr/services/mcp_activity.py tests/test_mcp_activity.py
git commit -m "feat: add bounded MCP activity store"
```

---

### Task 2: Add private Activity ingestion and admin snapshot/SSE APIs

**Files:**
- Modify: `cptr/routers/mcp.py`
- Modify: `cptr/routers/gateway.py`
- Modify: `cptr/app.py`
- Create: `tests/test_mcp_activity_api.py`

**Interfaces:**
- Consumes `McpActivityBatch` and `mcp_activity_store` from Task 1.
- Produces:
  - `POST /api/mcp/activity/events`
  - `GET /api/mcp/activity/snapshot`
  - `GET /api/mcp/activity/stream`
  - default plugin scope `mcp:activity:write`.

- [ ] **Step 1: Write failing API/auth/SSE tests**

Cover these exact assertions:

```python
self.assertIn("mcp:activity:write", DEFAULT_CONTROL_SCOPES)
self.assertEqual(DEFAULT_CONTROL_SCOPES.count("mcp:activity:write"), 1)
```

Patch `authenticate_control_request` and assert ingestion calls it with `"mcp:activity:write"`. Missing/invalid token maps to 401; missing required scope maps to 403. Patch `require_admin` and prove snapshot/stream are browser-admin-only. Iterate the stream and assert the first frame is `event: snapshot`, then ingest one new activity record and assert the next frame is `event: activity`.

Add a contract test that encodes both traffic and activity snapshots and proves:

```python
assert "arguments_json" not in str(traffic_snapshot)
assert "result_json" not in str(traffic_snapshot)
assert "arguments_json" in str(activity_snapshot)
```

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest tests/test_mcp_activity_api.py -q
```

Expected: missing activity endpoint/import/scope failures.

- [ ] **Step 3: Add the dedicated scope and exact middleware pass-through**

Append exactly `"mcp:activity:write"` to `DEFAULT_CONTROL_SCOPES` in `cptr/routers/gateway.py`.

In `cptr/app.py`, extend only the existing plugin-ingestion POST bypass condition so the cookie middleware passes through these two exact paths:

```python
request.method == "POST" and request.url.path in {
    "/api/mcp/traffic/events",
    "/api/mcp/activity/events",
}
```

No GET route receives this bypass.

- [ ] **Step 4: Add Activity route helpers and endpoints in `cptr/routers/mcp.py`**

Use a separate helper:

```python
async def _require_activity_writer(request: Request) -> str:
    try:
        return await authenticate_control_request(request, "mcp:activity:write")
    except PermissionError as exc:
        if str(exc).startswith("missing required scope"):
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        raise HTTPException(status_code=401, detail="control-plane authentication failed") from exc
```

Use `_activity_sse()` with compact JSON and these handlers:

```python
@router.post("/activity/events")
async def ingest_mcp_activity(request: Request, body: McpActivityBatch): ...

@router.get("/activity/snapshot")
async def get_mcp_activity_snapshot(request: Request): ...

@router.get("/activity/stream")
async def stream_mcp_activity(request: Request): ...
```

Subscribe inside the stream generator, not before response iteration. Emit an initial snapshot, incremental `activity` frames, 15-second keepalive comments, and unsubscribe in `finally`.

- [ ] **Step 5: Run Task 1+2 backend tests**

```bash
.venv/bin/python -m pytest tests/test_mcp_activity.py tests/test_mcp_activity_api.py tests/test_mcp_traffic.py tests/test_mcp_traffic_api.py -q
```

Expected: all pass.

- [ ] **Step 6: Run backend style/diff checks and commit**

```bash
.venv/bin/python -m ruff check cptr/services/mcp_activity.py cptr/routers/mcp.py cptr/routers/gateway.py cptr/app.py tests/test_mcp_activity.py tests/test_mcp_activity_api.py
.venv/bin/python -m ruff format --check cptr/services/mcp_activity.py cptr/routers/mcp.py cptr/routers/gateway.py cptr/app.py tests/test_mcp_activity.py tests/test_mcp_activity_api.py
git diff --check
git add cptr/services/mcp_activity.py cptr/routers/mcp.py cptr/routers/gateway.py cptr/app.py tests/test_mcp_activity.py tests/test_mcp_activity_api.py
git commit -m "feat: expose live MCP activity API"
```

---

### Task 3: Add the plugin Activity emitter and CPTR delivery client

**Files:**
- Create: `server/mcp-activity.ts`
- Modify: `server/client/computer-client.ts`
- Create: `tests/mcp-activity.test.ts`

**Interfaces:**
- Produces `McpActivityEvent`, `McpActivityEmitter`, and `ComputerClient.ingestMcpActivity(events)`.
- `McpActivityEmitter.started(input)`, `.complete(input)`, `.failed(input)`, `.flush()`, `.close()`.
- Consumes existing `TrafficClient` / `mcpRequestContext` client correlation semantics but does not alter topology traffic.

- [ ] **Step 1: Create a clean plugin feature branch/worktree before edits**

From a clean plugin checkout at current `origin/main`:

```bash
git switch -c feature/mcp-live-activity origin/main
```

If using a CPTR Direct Coding Worker, create the worker from the clean plugin repository instead of switching a dirty checkout.

- [ ] **Step 2: Write failing producer/client tests**

Test a tiny emitter with `maxQueue=2`, `batchSize=2`, and a fake `deliver` function. Assert:

- three enqueued events leave at most two queued and increment dropped count;
- delivered event copies contain only the exact activity envelope keys;
- each payload string is capped at 13_000 characters;
- delivery rejection increments dropped count but does not throw from `.started/.complete/.failed`;
- `ComputerClient.ingestMcpActivity()` posts to `/api/mcp/activity/events` with the existing bearer token and JSON body `{ events }`;
- 401/403/5xx responses become bounded `ComputerApiError` envelopes without response-body leakage.

- [ ] **Step 3: Verify RED**

```bash
npm test -- --test-name-pattern="MCP activity|activity ingestion"
```

Expected: missing module/method failures.

- [ ] **Step 4: Implement `server/mcp-activity.ts`**

Use the exact event shape from the spec. Reuse `TrafficClient` from `mcp-traffic.ts` rather than defining a second client identity model.

The emitter constructor reads bounded environment values:

```text
CPTR_MCP_ACTIVITY_PLUGIN_BATCH_SIZE   default 20, range 1..100
CPTR_MCP_ACTIVITY_PLUGIN_FLUSH_MS     default 250, range 25..10000
CPTR_MCP_ACTIVITY_PLUGIN_MAX_QUEUE    default 500, range 10..5000
```

Every emission creates a UUID `event_id`, monotonically increasing process sequence, `Date.now()` timestamp, and a copy with fixed allowlisted fields. `boundedText()` must cap title/summary/payloads to the server limits.

- [ ] **Step 5: Implement `ComputerClient.ingestMcpActivity()`**

Mirror `ingestMcpTraffic()` but target:

```text
POST ${baseUrl}/api/mcp/activity/events
```

Use existing token, timeout, `Accept: application/json`, and `Content-Type: application/json`. Error messages must be generic:

```text
mcp_activity_ingestion_failed
mcp_activity_timeout
mcp_activity_unavailable
```

Do not include CPTR response bodies in the thrown error.

- [ ] **Step 6: Run focused tests + typecheck**

```bash
npm test -- --test-name-pattern="MCP activity|activity ingestion"
npm run typecheck
```

Expected: pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add server/mcp-activity.ts server/client/computer-client.ts tests/mcp-activity.test.ts
git commit -m "feat: add MCP activity delivery"
```

---

### Task 4: Instrument every registered plugin MCP action with authoritative Activity

**Files:**
- Modify: `server/index.ts`
- Modify: `server/mcp.ts`
- Modify: `tests/mcp-activity.test.ts`
- Modify: existing MCP contract/activity tests as needed

**Interfaces:**
- Consumes `McpActivityEmitter` from Task 3.
- `createMcpServer(..., { activityTelemetry })` receives one emitter reference.
- Every actual registered action emits exactly one `started` and one terminal `complete` or `failed` Activity record.

- [ ] **Step 1: Add failing wrapper/correlation/failure-isolation tests**

Create tests that invoke a real registered test tool through the same `registerTool` wrapper and assert ordered records:

```text
started: arguments_json present, result_json/error_json absent
complete: result_json present, duration_ms >= 0
```

For a throwing handler:

```text
started
failed: error_json present, result_json absent
```

Assert worker-scoped direct-coding tool calls also emit normalized Activity even though they continue to publish `direct.worker` Workbench activity.

Use an AsyncLocalStorage request context with client `ChatGPT`, session `session-1`, request `request-1` and assert those fields survive into both records.

Configure `activityTelemetry` delivery to reject and assert the underlying MCP handler's success result is unchanged.

- [ ] **Step 2: Verify RED**

```bash
npm test -- --test-name-pattern="MCP activity"
```

Expected: wrapper does not yet emit new Activity records.

- [ ] **Step 3: Instantiate the emitter in `server/index.ts`**

Create one process-level emitter:

```ts
const mcpActivity = new McpActivityEmitter({
  deliver: (events) => client.ingestMcpActivity(events),
});
```

Pass it into every `createMcpServer()` path—stateful session server and stateless compatibility server—through option name `activityTelemetry`.

On shutdown, `await mcpActivity.close()` alongside existing transport cleanup. Activity close failures must be caught/logged generically and must not prevent server shutdown.

- [ ] **Step 4: Emit from the existing one-time `registerTool` wrapper in `server/mcp.ts`**

Extend the `createMcpServer` options type with:

```ts
activityTelemetry?: McpActivityEmitter;
```

Immediately after deriving `label`, `input`, and `trafficContext`, compute the already-redacted input once:

```ts
const activityArgumentsJson = terminalJson(input);
```

Call:

```ts
options.activityTelemetry?.started({
  client: trafficContext?.client,
  sessionId: trafficContext?.sessionId ?? null,
  requestId: trafficContext?.requestId ?? null,
  toolName: name,
  title: label,
  summary: `Working: ${label}.`,
  argumentsJson: activityArgumentsJson,
});
```

After successful handler completion, use exactly:

```ts
const activityResultJson = terminalJson(terminalToolResult(value));
```

and emit `complete` with elapsed time.

On failure, use the same normalized envelope already used by `publishActivity`, then:

```ts
const activityErrorJson = terminalJson(envelope);
```

and emit `failed` with elapsed time.

Do not remove or weaken existing prompt-session `publishActivity` / `direct.worker` behavior.

- [ ] **Step 5: Run full plugin contract gate**

```bash
npm test
npm run typecheck
npm run build
git diff --check
```

Expected: all plugin tests pass and ChatGPT-visible tool contract/count remains unchanged.

- [ ] **Step 6: Commit Task 4**

```bash
git add server/index.ts server/mcp.ts tests/mcp-activity.test.ts tests

git commit -m "feat: stream real MCP tool activity"
```

Before committing, stage only files actually changed by Task 4; do not stage generated assets or dependency directories.

---

### Task 5: Add frontend Activity API/reducer/feed and unify manual Console invocation presentation

**Files:**
- Modify: `cptr/frontend/src/lib/apis/mcp.ts`
- Create: `cptr/frontend/src/lib/stores/mcp-activity.ts`
- Create: `cptr/frontend/src/lib/components/mcp/McpActivityFeed.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpConsole.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpCallCard.svelte`
- Modify: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

**Interfaces:**
- Produces typed `getMcpActivitySnapshot()` and `openMcpActivityStream()` browser helpers.
- Produces a pure reducer that folds started/terminal phases into bounded activity rows.
- `McpConsole` no longer treats local `callLog` as the authoritative Activity source.

- [ ] **Step 1: Write failing frontend contract/behavior tests**

Add source assertions for:

- `/api/mcp/activity/snapshot` and `/api/mcp/activity/stream` helpers;
- `McpActivityFeed.svelte` exists and is rendered in Console Activity pane;
- Activity clear is browser-presentation-only;
- mobile `Servers | Activity | Tool` navigation remains;
- local manual invocation is labeled `Console invocation`, not ChatGPT;
- Activity rows render Input + Output/Error using bounded strings.

Add executable TypeScript reducer tests using Node's native TS loader for this sequence:

```text
started(event_id=a, request_id=r1, tool=cptr_list_workspaces)
complete(event_id=b, request_id=r1, result_json={...})
```

Expected: one row with phase `complete`, preserved input, terminal output, duration, ChatGPT client label.

A `failed` terminal event must produce error state and retain the started input.

- [ ] **Step 2: Verify RED**

```bash
node --test cptr/frontend/tests/mcp-traffic-topology.test.mjs
```

Expected: missing Activity API/store/feed assertions fail.

- [ ] **Step 3: Add Activity types/API helpers in `mcp.ts`**

Define:

```ts
export interface McpActivityEvent { ... }
export interface McpActivitySnapshot {
  version: 1;
  sequence: number;
  events: McpActivityEvent[];
  stream_health: {
    subscriber_count: number;
    slow_subscriber_drops: number;
    event_capacity: number;
  };
}
```

`openMcpActivityStream()` uses same-origin `EventSource`, event names `snapshot` and `activity`, and returns a close function.

- [ ] **Step 4: Create the pure bounded reducer**

`McpActivityState` stores:

```ts
sequence: number;
eventCapacity: number;
rows: McpActivityRow[];
seenEventIds: string[];
```

`McpActivityRow` contains client, tool/title, phase, started/completed timestamps, duration, input/result/error JSON, request/session IDs, and `source: 'plugin' | 'console'`.

Fold plugin `started` + terminal events by `request_id + tool_name`; if `request_id` is absent, fall back to event ID so records never merge unrelated calls. Ignore duplicate/stale ingestion sequences. Bound rows and seen IDs to snapshot capacity.

Provide a helper to merge a local `Console invocation` row without pretending it is plugin-origin.

- [ ] **Step 5: Create `McpActivityFeed.svelte`**

Responsibilities:

- fetch snapshot on mount;
- open SSE;
- show `live / reconnecting` indicator;
- exponential bounded reconnect delays `[1000, 2000, 4000, 8000]` ms;
- resnapshot before reopening stream;
- maintain operator scroll position: auto-follow only when within 48 px of bottom;
- Clear sets a local `hiddenBeforeSequence`/presentation cursor and clears local console records; Refresh resnapshots and restores server history;
- render each row through tokenized call-card presentation.

- [ ] **Step 6: Refactor `McpConsole.svelte`**

Keep server/tool selection and `invokeToolStreaming()` behavior. Replace the center local-only call log with `McpActivityFeed` plus local invocation callback state.

For manual invocation create a local row with:

```text
source = "console"
clientLabel = "Console invocation"
```

Update it from `tool_chunk`, `tool_done`, and `tool_error`. Do not call it ChatGPT activity.

- [ ] **Step 7: Make `McpCallCard.svelte` accept unified activity presentation**

Extract or adapt props so the same component can render:

- live plugin started/complete/failed Activity;
- local console streaming call.

All surfaces use `app-surface`, `app-subtle-surface`, `app-muted`, `app-accent-surface` or CSS variables; no fixed `bg-white`/`dark:bg-gray-*` is introduced.

- [ ] **Step 8: Run frontend tests/build and commit**

```bash
node --test cptr/frontend/tests/mcp-traffic-topology.test.mjs
cd cptr/frontend
npx prettier --check src/lib/apis/mcp.ts src/lib/stores/mcp-activity.ts src/lib/components/mcp/McpActivityFeed.svelte src/lib/components/mcp/McpConsole.svelte src/lib/components/mcp/McpCallCard.svelte tests/mcp-traffic-topology.test.mjs
npm run build
cd ../..
git diff --check
git add cptr/frontend/src/lib/apis/mcp.ts cptr/frontend/src/lib/stores/mcp-activity.ts cptr/frontend/src/lib/components/mcp/McpActivityFeed.svelte cptr/frontend/src/lib/components/mcp/McpConsole.svelte cptr/frontend/src/lib/components/mcp/McpCallCard.svelte cptr/frontend/tests/mcp-traffic-topology.test.mjs
git commit -m "feat: show real MCP tool activity"
```

---

### Task 6: Finish NightOwl tokenization, Back navigation, compact Recent Requests, and full-path topology

**Files:**
- Modify: `cptr/frontend/src/routes/mcp/+page.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpTopology.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpTopologyGraph.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpRecentRequests.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpConsole.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpServerList.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpToolForm.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpCallCard.svelte`
- Modify: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

**Interfaces:**
- Preserves existing topology reducer/API semantics.
- Adds presentation-only fixed infrastructure nodes; they never enter traffic state or request counts.

- [ ] **Step 1: Add failing theme/navigation/mobile/topology tests**

Assert the `/mcp` route contains a Back link/button with `href="/"` or equivalent Svelte navigation and accessible label `Back to CPTR Home`.

Assert MCP component source uses appearance utilities/tokens and does not retain page-defining combinations such as:

```text
bg-white/80
bg-white/70
bg-gray-50/70
min-w-[39rem]
```

Assert the graph source renders fixed labels:

```text
MCP Connector
CPTR MCP
CPTR Backend
```

and dynamic `Unknown MCP Client` still comes only from telemetry node data.

Assert Recent Requests has a mobile list hidden at desktop and a desktop table hidden on mobile.

- [ ] **Step 2: Verify RED**

```bash
node --test cptr/frontend/tests/mcp-traffic-topology.test.mjs
```

- [ ] **Step 3: Tokenize the route shell/header/tabs and add Back**

Use `app-theme`, `app-surface`, `app-raised-surface`, `app-subtle-surface`, `app-interactive`, `app-interactive-active`, `app-muted`, and `app-accent` helpers from `app.css` instead of hard-coded light/dark backgrounds.

Back control:

```svelte
<a href="/" aria-label="Back to CPTR Home" class="app-interactive flex min-h-11 min-w-11 items-center justify-center rounded-xl sm:min-h-9 sm:min-w-9">...</a>
```

Keep the title and tabs on one compact mobile row without overflow.

- [ ] **Step 4: Tokenize topology and Console surfaces**

Replace page-defining `bg-white`, `bg-gray-*`, and dark-pair classes with appearance utilities/CSS variables in the MCP components listed above. Semantic colors remain allowed only for success/warning/error and request-pulse status.

Inputs use:

```text
background: var(--app-surface-subtle)
color: var(--app-fg)
border-color: var(--app-border)
focus ring: var(--app-focus-ring)
```

Selected rows/tabs use `var(--app-active)` / `var(--app-accent-soft)`.

- [ ] **Step 5: Implement mobile Recent Requests list**

Keep the existing desktop table inside `hidden sm:block` or equivalent. Add a `sm:hidden` list where each button row contains:

```text
line 1: client label                 status · when
line 2: tool/method                  input bytes / output bytes
```

The entire row is a >=44 px target and toggles the same safe request detail state. No horizontal scrolling or desktop minimum width exists in the mobile branch.

- [ ] **Step 6: Add fixed infrastructure nodes and full path edges**

Keep dynamic client coordinates from `topologyNodes(state)`. Presentation geometry in `McpTopologyGraph.svelte` uses fixed positions:

```text
MCP Connector: centerX, centerY - 120
CPTR MCP:      centerX, centerY
CPTR Backend:  centerX, centerY + 120
```

Dynamic clients remain in an outer arc/radial region above/around the connector. Each dynamic client edge terminates at MCP Connector. Add fixed Connector→CPTR MCP and CPTR MCP→Backend edges.

When any client is active/pulsing, highlight all three path segments and animate particles in order. `prefers-reduced-motion` hides particles and uses static highlighted edges.

Do not add connector/backend to `state.clients` or synthetic request counters.

- [ ] **Step 7: Run frontend regression + build**

```bash
node --test cptr/frontend/tests/*.test.mjs
cd cptr/frontend
npx prettier --check src/lib/components/mcp src/routes/mcp/+page.svelte src/lib/apis/mcp.ts src/lib/stores/mcp-activity.ts tests
npm run build
cd ../..
git diff --check
```

Expected: all frontend regressions pass and production build succeeds.

- [ ] **Step 8: Commit Task 6**

```bash
git add cptr/frontend/src/routes/mcp/+page.svelte cptr/frontend/src/lib/components/mcp cptr/frontend/tests/mcp-traffic-topology.test.mjs
git commit -m "fix: align MCP UI with NightOwl"
```

---

### Task 7: Audit and regression-test every Console function

**Files:**
- Modify: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`
- Create if needed: `cptr/frontend/tests/mcp-console-functions.test.mjs`
- Modify only MCP UI/API files when a failing functional test exposes a real defect.

**Interfaces:**
- No new feature contract; this task validates/fixes the existing Console inspector behavior required by the spec.

- [ ] **Step 1: Add explicit regression coverage for all Console controls**

Tests must cover source/behavior contracts for:

1. server list loads from `listMcpServers()`;
2. health status is rendered;
3. expand/collapse calls `listServerTools()` once per uncached server;
4. Refresh invokes `loadServers()`;
5. reconnect calls `reconnectMcpServer()` and reloads status/tools;
6. tool selection moves mobile view to Tool;
7. enum/boolean/number/object/array/string controls map to expected argument types;
8. raw JSON toggle preserves current form values;
9. invalid raw JSON sets visible `jsonError` and blocks invocation;
10. Invoke calls `invokeToolStreaming()`;
11. Ctrl/Cmd+Enter calls the same submit path exactly once;
12. `tool_chunk` appends streaming result content;
13. `tool_done` marks local console row complete;
14. `tool_error` marks row failed and surfaces a toast/error;
15. text/image/resource result types remain renderable;
16. activity/request cards expand/collapse with keyboard activation;
17. mobile Servers/Activity/Tool buttons display one pane only;
18. Activity reconnect resnapshots before stream reopen;
19. Clear is presentation-only and Refresh restores server snapshot history;
20. Admin links remain `/admin`.

Where full DOM behavior is impractical in the existing Node source-regression harness, test pure exported helpers in a small utility module instead of asserting fragile source strings.

- [ ] **Step 2: Run the new Console functional suite and verify any RED failures**

```bash
node --test cptr/frontend/tests/mcp-console-functions.test.mjs cptr/frontend/tests/mcp-traffic-topology.test.mjs
```

- [ ] **Step 3: Fix only defects proven by the tests**

Examples of acceptable fixes:

- reset/reload logic that leaves an expanded server collapsed after reconnect;
- numeric/boolean coercion bugs;
- malformed SSE final-buffer handling;
- duplicate Ctrl+Enter submit;
- silent API error paths;
- mobile pane switching inconsistencies.

Do not refactor unrelated frontend code.

- [ ] **Step 4: Run all frontend MCP regressions + production build**

```bash
node --test cptr/frontend/tests/*.test.mjs
cd cptr/frontend
npx prettier --check src/lib/components/mcp src/lib/apis/mcp.ts src/lib/stores/mcp-activity.ts tests
npm run build
cd ../..
git diff --check
```

- [ ] **Step 5: Commit Task 7**

```bash
git add cptr/frontend/src/lib/components/mcp cptr/frontend/src/lib/apis/mcp.ts cptr/frontend/src/lib/stores/mcp-activity.ts cptr/frontend/tests
git commit -m "test: harden MCP console functions"
```

Stage only files actually changed by this task.

---

### Task 8: Real two-channel MCP integration acceptance

**Files:**
- Create or modify: `chatgpt-computer-plugin/scripts/check-mcp-live-activity-integration.mjs`
- Modify: `chatgpt-computer-plugin/package.json` only for a named script
- Test/support code only as required for a disposable local harness

**Interfaces:**
- Proves the real plugin transport emits both metadata-only topology traffic and payload-bearing redacted Activity for the same MCP request/tool call.

- [ ] **Step 1: Add an env-driven integration verifier**

The verifier must use a real `@modelcontextprotocol/sdk` Streamable HTTP client with clientInfo:

```text
name = ChatGPT Acceptance
version = 1.0.0
```

It must invoke a real safe MCP tool such as `cptr_list_workspaces` and terminate the protocol session explicitly.

It reads CPTR traffic and activity snapshots from a disposable authenticated harness or disposable CPTR instance and asserts:

```text
traffic: ChatGPT client exists
traffic: tools/call or tool lifecycle exists
traffic: no arguments_json/result_json/error_json keys
activity: started record exists for cptr_list_workspaces
activity: complete record exists for same request/tool
activity started: arguments_json is present and bounded
activity complete: result_json is present and bounded
activity: no bearer/API token sentinel appears
session close observed in traffic
```

- [ ] **Step 2: Add failure-isolation acceptance**

Run a second plugin/harness path where `/api/mcp/activity/events` returns 403 or 503 while normal CPTR tool APIs remain available. Assert the real MCP tool call still succeeds and the plugin does not return Activity-delivery failure to the MCP client.

- [ ] **Step 3: Add named package script**

```json
"check:mcp-live-activity": "node scripts/check-mcp-live-activity-integration.mjs"
```

- [ ] **Step 4: Run plugin acceptance + complete plugin gate**

```bash
npm run check:mcp-live-activity
npm test
npm run typecheck
npm run build
git diff --check
```

Expected: real integration acceptance passes, full plugin tests pass, typecheck/build pass.

- [ ] **Step 5: Commit Task 8**

```bash
git add scripts/check-mcp-live-activity-integration.mjs package.json tests server
git commit -m "test: verify live MCP activity end to end"
```

Stage only files actually changed by this task.

---

### Task 9: Final Computer gate, mobile/desktop NightOwl browser verification, and runtime proof

**Files:**
- No new product files unless verification finds a regression.
- Temporary browser fixtures/harnesses must remain untracked and be removed after use.

**Interfaces:**
- Produces final acceptance evidence; no new public API.

- [ ] **Step 1: Run the complete Computer backend MCP gate**

```bash
.venv/bin/python -m pytest tests/test_mcp_traffic.py tests/test_mcp_traffic_api.py tests/test_mcp_activity.py tests/test_mcp_activity_api.py -q
.venv/bin/python -m ruff check cptr/services/mcp_traffic.py cptr/services/mcp_activity.py cptr/routers/mcp.py cptr/routers/gateway.py cptr/app.py tests/test_mcp_traffic.py tests/test_mcp_traffic_api.py tests/test_mcp_activity.py tests/test_mcp_activity_api.py
.venv/bin/python -m ruff format --check cptr/services/mcp_traffic.py cptr/services/mcp_activity.py cptr/routers/mcp.py cptr/routers/gateway.py cptr/app.py tests/test_mcp_traffic.py tests/test_mcp_traffic_api.py tests/test_mcp_activity.py tests/test_mcp_activity_api.py
```

- [ ] **Step 2: Run complete frontend regressions/build**

```bash
node --test cptr/frontend/tests/*.test.mjs
cd cptr/frontend
npx prettier --check src/lib/components/mcp src/lib/apis/mcp.ts src/lib/stores/mcp-traffic.ts src/lib/stores/mcp-activity.ts src/routes/mcp/+page.svelte tests
npm run build
cd ../..
git diff --check
```

- [ ] **Step 3: Verify NightOwl desktop UI in managed Chrome**

Run the production-built `/mcp` page against a disposable authenticated local fixture that serves real-shaped traffic/activity snapshot+SSE data without modifying product source.

At a desktop viewport verify:

- deep NightOwl background/surfaces derive from active appearance tokens;
- Back, MCP title, Topology/Console tabs fit cleanly;
- graph shows ChatGPT → MCP Connector → CPTR MCP → CPTR Backend;
- active request lights the complete path;
- desktop Recent Requests table is visible;
- Console shows authoritative ChatGPT Activity Input + Output;
- Servers and Tool panes remain visible in desktop three-pane layout.

Capture screenshots.

- [ ] **Step 4: Verify 390×844 mobile UI**

Verify:

- no white/washed-out MCP page surfaces under NightOwl;
- Back button is visible and >=44 px target;
- Topology/Console header does not clip;
- graph fits width;
- Recent Requests is a compact two-line list with no horizontal scroll;
- Console `Servers | Activity | Tool` shows one full-width pane at a time;
- Activity Input/Output can be expanded without horizontal page overflow.

Capture screenshots.

- [ ] **Step 5: Verify real runtime Activity with a fresh MCP session**

After local integration gates pass, use a fresh real MCP client connection against the intended runtime and invoke one safe tool. Verify server-side snapshot output prints only sanitized evidence:

```text
client label
activity tool name
phase sequence
bounded payload lengths
traffic HTTP status
activity HTTP status
```

Never print tokens/cookies/raw credentials.

- [ ] **Step 6: Verify existing appearance/navigation regression boundaries**

Open CPTR Home and one non-MCP surface and confirm their NightOwl appearance remains unchanged. Back from `/mcp` returns to Home.

- [ ] **Step 7: Final repository audit**

For each repository run:

```bash
git status --short --branch
git log -5 --oneline
git diff --check
```

Expected: clean working trees after commits; no generated fixtures or dependency symlinks staged.

- [ ] **Step 8: Report exact evidence**

Report:

- Computer branch + exact head SHA;
- plugin branch + exact head SHA;
- backend/frontend/plugin test counts;
- production build/typecheck status;
- real Activity acceptance result;
- browser desktop/mobile screenshots;
- any pre-existing unrelated warnings separately;
- whether the new commits are local-only or pushed.

Do not claim deployment complete unless the user separately asks for deployment and live services are actually updated and reverified.
