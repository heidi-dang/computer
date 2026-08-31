# MCP Traffic Topology Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a real-time MCP traffic topology that places CPTR MCP at the center, renders ChatGPT/Claude/Gemini/other MCP clients around it, and animates only real inbound MCP requests observed by the ChatGPT-facing adapter.

**Architecture:** The `chatgpt-computer-plugin` adapter is the authoritative telemetry producer because it owns inbound Streamable HTTP MCP sessions and registered tool execution. It sends allowlisted, bounded, best-effort telemetry batches through the existing authenticated `ComputerClient` to an in-memory CPTR `McpTrafficStore`; CPTR exposes snapshot + SSE APIs and the merged `/mcp` page renders a pure-reducer-driven SVG topology beside a bounded recent-request panel. The two Git roots remain independent and produce coordinated PRs.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, asyncio, Svelte 5, TypeScript, SVG/CSS, Node test runner, `@modelcontextprotocol/sdk`, AsyncLocalStorage, existing CPTR bearer-token auth.

**Spec:** `docs/superpowers/specs/2026-08-31-mcp-traffic-topology-design.md`

## Global Constraints

- Real inbound MCP transport activity is authoritative; never infer topology activity from `/mcp` console manual test calls and never fabricate request pulses.
- CPTR MCP is the center node; ChatGPT, Claude, Gemini, Codex, MCP Inspector, and unknown compatible clients are outer nodes.
- Do not record or stream authorization headers, cookies, bearer tokens, API keys, full headers, raw tool arguments/results, prompts, filesystem paths extracted from arguments, chain-of-thought, or arbitrary exception strings.
- V1 is in-memory and bounded: no database persistence, external telemetry vendor, graph-layout dependency, WebGL, or Canvas dependency.
- Telemetry delivery is asynchronous and best-effort; telemetry failure must never fail or delay the primary MCP request path materially.
- Plugin event IDs are deduplicated by CPTR; CPTR assigns its own monotonic ingestion sequence for browser replay.
- Browser snapshot and stream require existing admin access. Plugin ingestion requires a bearer scope dedicated to telemetry ingestion.
- Animations must respect `prefers-reduced-motion`.
- Configuration must be environment/config driven; do not hard-code hostnames, ports, credentials, client labels as routing policy, models, usernames, or filesystem paths.
- `computer` branch: `feature/mcp-traffic-topology`, based on the latest `origin/main` containing merged PR #11.
- `chatgpt-computer-plugin` branch: `feature/mcp-traffic-telemetry`, created from that repository's latest clean `origin/main` at execution time.
- Existing MCP console behavior, ChatGPT-visible MCP tool count/schema, NightOwl UI, and current CI gates must remain unchanged except for the new Topology view and private telemetry API.

---

## File map

### `computer` repository

- Create `cptr/services/mcp_traffic.py` — V1 event schema, sanitization, dedupe, bounded ring buffer, active-session/client aggregates, subscriber fan-out, stale-session expiry.
- Modify `cptr/routers/mcp.py` — private telemetry ingestion + admin snapshot/SSE endpoints only; keep downstream-server inspection routes intact.
- Modify `cptr/routers/gateway.py` — add the dedicated `mcp:traffic:write` scope to default control-token scopes for newly issued plugin credentials.
- Create `tests/test_mcp_traffic.py` — store/schema/security/bounds/dedupe/session tests.
- Create `tests/test_mcp_traffic_api.py` — ingestion auth, browser admin auth, snapshot, SSE, slow-subscriber behavior.
- Modify `cptr/frontend/src/lib/apis/mcp.ts` — traffic types + snapshot/SSE helpers.
- Create `cptr/frontend/src/lib/stores/mcp-traffic.ts` — pure reducer and deterministic node layout projection.
- Create `cptr/frontend/src/lib/components/mcp/McpTopology.svelte` — snapshot/SSE lifecycle, graph + responsive shell.
- Create `cptr/frontend/src/lib/components/mcp/McpTopologyGraph.svelte` — SVG node/edge/pulse renderer only.
- Create `cptr/frontend/src/lib/components/mcp/McpRecentRequests.svelte` — bounded request rows and safe detail UI.
- Modify `cptr/frontend/src/routes/mcp/+page.svelte` — Topology/Console switch; Topology default, Console preserved.
- Create `cptr/frontend/tests/mcp-traffic-topology.test.mjs` — source-contract/reducer/UI regression coverage.

### `chatgpt-computer-plugin` repository

- Create `server/mcp-traffic.ts` — event types, client normalization, AsyncLocalStorage request context, bounded batching queue, safe error-code mapping, byte counting, best-effort delivery.
- Modify `server/client/computer-client.ts` — `ingestMcpTraffic(events)` using the existing CPTR base URL/token and `/api/mcp/traffic/events`.
- Modify `server/index.ts` — initialize/session/request instrumentation and queue lifecycle.
- Modify `server/mcp.ts` — emit tool start/terminal events from the existing one-time `registerTool` wrapper, using request AsyncLocalStorage correlation.
- Create `tests/mcp-traffic.test.ts` — normalization, batching/bounds, redaction, request correlation, failure isolation.
- Modify `tests/mcp.test.ts` and/or `tests/activity-contract.test.ts` only if needed to assert unchanged ChatGPT-visible tool contract.
- Create `scripts/check-mcp-traffic-integration.mjs` — env-driven production-like verifier for two synthetic MCP clients against a running plugin + CPTR pair.

---

### Task 1: CPTR bounded MCP traffic store and schema

**Files:**
- Create: `cptr/services/mcp_traffic.py`
- Create: `tests/test_mcp_traffic.py`

**Interfaces:**
- Produces: `McpTrafficEvent`, `McpTrafficBatch`, `McpTrafficStore`, and singleton `mcp_traffic_store`.
- `await McpTrafficStore.ingest(events: list[McpTrafficEvent]) -> dict[str, int]`
- `await McpTrafficStore.snapshot() -> dict[str, object]`
- `McpTrafficStore.subscribe() -> asyncio.Queue[dict[str, object]]`
- `McpTrafficStore.unsubscribe(queue) -> None`
- `await McpTrafficStore.expire_stale_sessions(now_ms: int | None = None) -> int`

- [ ] **Step 1: Write failing store tests**

Create tests that construct the store with tiny explicit bounds and prove dedupe, ring truncation, session aggregation, terminal request counters, unknown-field rejection, label bounds, and stale expiry. Use concrete V1 events such as:

```python
from cptr.services.mcp_traffic import McpTrafficEvent, McpTrafficStore


def event(event_id: str, event_type: str, *, session_id="s-1", status="started"):
    return McpTrafficEvent(
        version=1,
        event_id=event_id,
        sequence=1,
        event_type=event_type,
        timestamp_ms=1_788_000_000_000,
        session_id=session_id,
        client={"id": "chatgpt", "label": "ChatGPT", "version": "1.0"},
        request_id="r-1",
        method="tools/call",
        tool_name="cptr_list_workspaces",
        status=status,
        duration_ms=None,
        request_bytes=120,
        response_bytes=None,
        error_code=None,
    )


async def test_duplicate_event_id_is_ignored():
    store = McpTrafficStore(max_events=8, max_sessions=4, subscriber_queue_size=2)
    first = event("evt-1", "request_started")
    result = await store.ingest([first, first])
    assert result == {"accepted": 1, "duplicates": 1, "dropped": 0}
    assert len((await store.snapshot())["events"]) == 1
```

Also assert Pydantic validation rejects a label over the configured schema limit and `extra="forbid"` rejects a synthetic `authorization` field.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_mcp_traffic.py -q
```

Expected: collection/import failure because `cptr.services.mcp_traffic` does not exist.

- [ ] **Step 3: Implement the minimal store**

Implement a strict event model and a store whose mutable state is protected by one `asyncio.Lock`. The model shape must match the spec exactly:

```python
class McpTrafficClient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=80)
    version: str | None = Field(default=None, max_length=64)


class McpTrafficEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    event_id: str = Field(min_length=8, max_length=128)
    sequence: int = Field(ge=1)
    event_type: Literal[
        "session_opened", "session_closed", "request_started", "request_finished",
        "request_failed", "tool_started", "tool_finished", "tool_failed",
    ]
    timestamp_ms: int = Field(ge=0)
    session_id: str | None = Field(default=None, max_length=128)
    client: McpTrafficClient
    request_id: str | None = Field(default=None, max_length=128)
    method: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, max_length=256)
    status: Literal["started", "complete", "error", "connected", "disconnected"]
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    request_bytes: int | None = Field(default=None, ge=0, le=100_000_000)
    response_bytes: int | None = Field(default=None, ge=0, le=100_000_000)
    error_code: Literal[
        "timeout", "validation_error", "unauthorized", "tool_error",
        "transport_error", "internal_error",
    ] | None = None
```

Use `deque(maxlen=max_events)` for recent events and a second bounded deque/set pair for dedupe IDs. Subscriber delivery must use `put_nowait`; on `QueueFull`, remove one oldest queued item and retry once, incrementing a slow-subscriber drop counter instead of blocking ingestion.

- [ ] **Step 4: Run tests and verify GREEN**

```bash
.venv/bin/python -m pytest tests/test_mcp_traffic.py -q
```

Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add cptr/services/mcp_traffic.py tests/test_mcp_traffic.py
git commit -m "feat: add bounded MCP traffic store"
```

---

### Task 2: CPTR private ingestion, admin snapshot, and SSE API

**Files:**
- Modify: `cptr/routers/mcp.py`
- Modify: `cptr/routers/gateway.py`
- Create: `tests/test_mcp_traffic_api.py`

**Interfaces:**
- Consumes: `mcp_traffic_store` and `McpTrafficBatch` from Task 1.
- Produces:
  - `POST /api/mcp/traffic/events`
  - `GET /api/mcp/traffic/snapshot`
  - `GET /api/mcp/traffic/stream`
  - control scope `mcp:traffic:write` for newly issued plugin tokens.

- [ ] **Step 1: Write failing API tests**

Test the ingestion path with a request object carrying `Authorization: Bearer ...` and patch `authenticate_control_request` to prove the route requests `mcp:traffic:write`. Test missing/invalid scope maps to 401/403. Test snapshot/stream use `require_admin(request)` rather than bearer-only plugin auth.

For SSE, instantiate a fresh store, call the route, read its first `snapshot` event, ingest a second event, then assert the next SSE frame contains the new ingestion sequence and normalized event without arguments/results.

- [ ] **Step 2: Verify RED**

```bash
.venv/bin/python -m pytest tests/test_mcp_traffic_api.py -q
```

Expected: endpoint/import failures.

- [ ] **Step 3: Add `mcp:traffic:write` to default control scopes**

In `cptr/routers/gateway.py`, extend `DEFAULT_CONTROL_SCOPES` with exactly one new non-browser telemetry-ingestion scope:

```python
DEFAULT_CONTROL_SCOPES = (
    "workspace:read",
    "task:read",
    "task:write",
    "autonomous:run",
    "git:read",
    "mcp:traffic:write",
)
```

Do not broaden unrelated scope checks.

- [ ] **Step 4: Implement the three traffic endpoints**

In `cptr/routers/mcp.py`, import `authenticate_control_request`, `mcp_traffic_store`, and `McpTrafficBatch`. Implement a small `_require_traffic_writer` helper mirroring control-plane error semantics. The ingestion endpoint must call `await authenticate_control_request(request, "mcp:traffic:write")`, never `require_admin`.

Browser routes must call `require_admin(request)` and return only safe store projections.

SSE frames should be generated with a helper equivalent to:

```python
def _traffic_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
```

The stream must send one `snapshot` frame immediately, then `traffic` frames from a bounded subscriber queue, plus `: keepalive\n\n` after a timeout. Always unsubscribe in `finally` and stop when `await request.is_disconnected()` is true.

- [ ] **Step 5: Run API + store tests**

```bash
.venv/bin/python -m pytest tests/test_mcp_traffic.py tests/test_mcp_traffic_api.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add cptr/routers/mcp.py cptr/routers/gateway.py tests/test_mcp_traffic_api.py
git commit -m "feat: expose MCP traffic telemetry API"
```

---

### Task 3: Frontend traffic types, reducer, and deterministic topology projection

**Files:**
- Modify: `cptr/frontend/src/lib/apis/mcp.ts`
- Create: `cptr/frontend/src/lib/stores/mcp-traffic.ts`
- Create: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

**Interfaces:**
- Produces `McpTrafficEvent`, `McpTrafficSnapshot`, `getMcpTrafficSnapshot()`, `openMcpTrafficStream(...)`.
- Produces pure functions `hydrateMcpTraffic`, `applyMcpTrafficEvent`, `topologyNodes`, and `recentRequestRows`.

- [ ] **Step 1: Add RED source-contract tests**

The existing frontend test suite uses Node source inspection. Add assertions that:

```js
assert.match(api, /getMcpTrafficSnapshot/);
assert.match(api, /openMcpTrafficStream/);
assert.match(store, /export function hydrateMcpTraffic/);
assert.match(store, /export function applyMcpTrafficEvent/);
assert.match(store, /export function topologyNodes/);
assert.match(store, /request_started/);
assert.match(store, /request_finished/);
assert.match(store, /request_failed/);
```

Also import the pure TypeScript logic through the project test-compatible path if available; otherwise keep reducer logic in a `.ts` module that can be exercised by the production build plus source-contract tests.

- [ ] **Step 2: Verify RED**

```bash
cd cptr/frontend
node --test tests/mcp-traffic-topology.test.mjs
```

Expected: failures for missing API/reducer symbols.

- [ ] **Step 3: Add typed API wrappers**

Add strict TypeScript interfaces matching the backend V1 envelope and snapshot. Implement snapshot via `fetchJSON('/api/mcp/traffic/snapshot')`.

Implement stream creation with native `EventSource('/api/mcp/traffic/stream')` because browser admin auth is cookie-based. Expose a cleanup function and callbacks for `snapshot`, `traffic`, and connection errors; do not put credentials or bearer tokens in query strings.

- [ ] **Step 4: Implement the pure reducer**

Keep transport/UI timers outside the reducer. Suggested state:

```ts
export type McpTrafficState = {
  sequence: number;
  events: McpTrafficEvent[];
  clients: Record<string, McpTrafficClientState>;
  activeRequests: Record<string, { clientId: string; startedAt: number; toolName?: string | null }>;
  seenEventIds: string[];
};
```

`applyMcpTrafficEvent` ignores `event.ingestion_sequence <= state.sequence` and duplicate IDs. `request_started` increments the correct client; terminal request events remove the matching active request, increment totals/errors, and project a recent row. Tool events update last active tool but do not double-count request totals.

`topologyNodes` sorts clients by stable `client.id`, assigns angle `(-Math.PI / 2) + (index * 2 * Math.PI / count)`, and returns normalized x/y positions. No random layout.

- [ ] **Step 5: Run focused tests + frontend build**

```bash
node --test tests/mcp-traffic-topology.test.mjs
npm run build
```

Expected: test pass and Vite production build exit 0.

- [ ] **Step 6: Commit Task 3**

```bash
git add cptr/frontend/src/lib/apis/mcp.ts cptr/frontend/src/lib/stores/mcp-traffic.ts cptr/frontend/tests/mcp-traffic-topology.test.mjs
git commit -m "feat: add MCP traffic frontend state"
```

---

### Task 4: SVG topology, recent requests, and `/mcp` Topology/Console views

**Files:**
- Create: `cptr/frontend/src/lib/components/mcp/McpTopologyGraph.svelte`
- Create: `cptr/frontend/src/lib/components/mcp/McpRecentRequests.svelte`
- Create: `cptr/frontend/src/lib/components/mcp/McpTopology.svelte`
- Modify: `cptr/frontend/src/routes/mcp/+page.svelte`
- Modify: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

**Interfaces:**
- Consumes Task 3 API/reducer only; graph components must not fetch directly.
- Produces desktop and mobile Topology view while preserving the current `McpConsole` component unchanged.

- [ ] **Step 1: Extend RED UI contract tests**

Require source-level evidence for:

```js
assert.match(page, /Topology/);
assert.match(page, /Console/);
assert.match(topology, /getMcpTrafficSnapshot/);
assert.match(topology, /openMcpTrafficStream/);
assert.match(graph, /<svg/);
assert.match(graph, /prefers-reduced-motion/);
assert.match(recent, /In \/ Out/);
assert.match(recent, /Status/);
assert.match(recent, /When/);
```

Also require responsive classes for desktop two-column and mobile stacked layouts.

- [ ] **Step 2: Verify RED**

```bash
cd cptr/frontend
node --test tests/mcp-traffic-topology.test.mjs
```

- [ ] **Step 3: Implement `McpTopologyGraph.svelte`**

Render one fixed center node labelled `CPTR MCP` and one SVG group per derived client node. Draw edges first, nodes second, transient pulse circles last. The active edge state is derived from active request counts; error flash state comes from the latest terminal event TTL maintained by `McpTopology.svelte`.

Use CSS keyframes only when motion is allowed. Include a component-level reduced-motion rule:

```css
@media (prefers-reduced-motion: reduce) {
  .traffic-particle,
  .center-ripple { animation: none !important; }
}
```

Clicking a client emits/selects its `clientId`; it does not mutate telemetry state.

- [ ] **Step 4: Implement `McpRecentRequests.svelte`**

Render columns `Client`, `Method / Tool`, `In / Out`, `Status`, `When`. Unknown sizes render `—`. A selected row opens an inline/detail pane containing only client label/version, method, tool, status, duration, bytes, normalized error code, shortened session ID, request ID, and timestamps.

Never render `arguments`, `result`, headers, token fields, or arbitrary backend error strings.

- [ ] **Step 5: Implement `McpTopology.svelte` snapshot/SSE lifecycle**

On mount:

1. fetch a fresh snapshot;
2. hydrate reducer state;
3. open SSE;
4. on SSE error set status to `reconnecting`, close the stream, wait with bounded 1s/2s/4s/8s backoff, fetch a new snapshot, then reopen;
5. on destroy cancel timers and close EventSource.

Use UI-only pulse/error decay timers keyed by request/event ID. These timers may make activity visible for roughly 600–1000 ms after completion but must never keep `activeRequests` logically active.

- [ ] **Step 6: Update `/mcp` page switcher**

Make `Topology` the initial view and `Console` the second view. Preserve the existing merged `McpConsole` component intact. Use existing NightOwl/app surface classes and 44px mobile touch targets.

- [ ] **Step 7: Run all frontend regressions and production build**

```bash
cd cptr/frontend
node --test tests/*.test.mjs
npx prettier --check src/lib/apis/mcp.ts src/lib/stores/mcp-traffic.ts src/lib/components/mcp/McpTopology.svelte src/lib/components/mcp/McpTopologyGraph.svelte src/lib/components/mcp/McpRecentRequests.svelte src/routes/mcp/+page.svelte tests/mcp-traffic-topology.test.mjs
npm run build
```

Expected: all Node tests pass, Prettier clean, build exit 0. Existing unrelated Svelte warnings may remain but no new error is accepted.

- [ ] **Step 8: Commit Task 4**

```bash
git add cptr/frontend/src/lib/components/mcp/McpTopology*.svelte cptr/frontend/src/lib/components/mcp/McpRecentRequests.svelte cptr/frontend/src/routes/mcp/+page.svelte cptr/frontend/tests/mcp-traffic-topology.test.mjs
git commit -m "feat: render live MCP traffic topology"
```

---

### Task 5: Plugin telemetry core, client normalization, queue bounds, and CPTR delivery

**Files:**
- Create in plugin repo: `server/mcp-traffic.ts`
- Modify in plugin repo: `server/client/computer-client.ts`
- Create in plugin repo: `tests/mcp-traffic.test.ts`

**Interfaces:**
- Produces `McpTrafficEmitter` with `sessionOpened`, `sessionClosed`, `requestStarted`, `requestFinished`, `requestFailed`, `toolStarted`, `toolFinished`, `toolFailed`, `flush`, `close`.
- Produces `mcpRequestContext: AsyncLocalStorage<{ requestId: string; sessionId: string | null; client: TrafficClient }>`.
- Adds `ComputerClient.ingestMcpTraffic(events: McpTrafficEvent[]): Promise<void>`.

- [ ] **Step 1: At execution time, create a clean plugin worktree/branch from latest `origin/main`**

The existing plugin checkout is dirty, so do not edit it directly. Fetch `origin/main`, create an isolated worktree on `feature/mcp-traffic-telemetry`, verify `git status --short` is empty, and record the base SHA in the final PR description.

- [ ] **Step 2: Write failing telemetry-core tests**

Tests must cover aliases and unknown labels:

```ts
assert.deepEqual(normalizeMcpClient({ name: "ChatGPT", version: "1" }), {
  id: "chatgpt",
  label: "ChatGPT",
  version: "1",
});
assert.equal(normalizeMcpClient({ name: "Claude Desktop" }).label, "Claude");
assert.equal(normalizeMcpClient({ name: "gemini-cli" }).label, "Gemini");
assert.equal(normalizeMcpClient({ name: "My Internal MCP Client" }).label, "My Internal MCP Client");
```

Queue tests use a fake delivery function that blocks/rejects. Prove emitting never awaits network, max queue length is respected, batches honor configured size, and payload JSON does not contain synthetic `authorization`, `arguments`, `result`, `prompt`, or filesystem-path fields.

- [ ] **Step 3: Verify RED**

```bash
npm test -- --test-name-pattern="MCP traffic"
```

Expected: missing module/functions.

- [ ] **Step 4: Implement `server/mcp-traffic.ts`**

Use only Node built-ins. Read bounds from environment with safe numeric parsing:

```ts
const batchSize = boundedInt(env.CPTR_MCP_TRAFFIC_PLUGIN_BATCH_SIZE, 20, 1, 100);
const flushMs = boundedInt(env.CPTR_MCP_TRAFFIC_PLUGIN_FLUSH_MS, 250, 25, 10_000);
const maxQueue = boundedInt(env.CPTR_MCP_TRAFFIC_PLUGIN_MAX_QUEUE, 1000, 10, 10_000);
```

Use `randomUUID()` for event/request IDs, one per-process event sequence, and `AsyncLocalStorage` for request correlation. Delivery must copy only explicit V1 fields into new objects before sending.

Normalize exceptions to the spec's public code set using error class/status/category—not raw error message text.

- [ ] **Step 5: Add `ComputerClient.ingestMcpTraffic`**

Because the telemetry endpoint is outside `/api/control/v1`, add a narrowly scoped helper that posts to `${baseUrl}/api/mcp/traffic/events` with the same bearer token. Do not expose the token in errors. Body shape:

```ts
{ events }
```

The method may throw `ComputerApiError`; the emitter catches it so MCP requests remain unaffected.

- [ ] **Step 6: Run plugin telemetry tests + typecheck**

```bash
npm test -- --test-name-pattern="MCP traffic"
npm run typecheck
```

- [ ] **Step 7: Commit Task 5 in plugin repo**

```bash
git add server/mcp-traffic.ts server/client/computer-client.ts tests/mcp-traffic.test.ts
git commit -m "feat: add MCP traffic telemetry emitter"
```

---

### Task 6: Instrument real plugin sessions, requests, and registered tool calls

**Files:**
- Modify in plugin repo: `server/index.ts`
- Modify in plugin repo: `server/mcp.ts`
- Modify in plugin repo: `tests/mcp-traffic.test.ts`
- Verify in plugin repo: `tests/mcp.test.ts`, `tests/activity-contract.test.ts`

**Interfaces:**
- Consumes `McpTrafficEmitter` + `mcpRequestContext` from Task 5.
- `createMcpServer` receives an optional telemetry emitter/context dependency rather than importing global mutable request state.

- [ ] **Step 1: Add RED lifecycle/correlation tests**

Use linked/in-memory MCP transport where possible and direct request-boundary unit tests where HTTP transport metadata is needed. Required assertions:

- stateful initialize emits one `session_opened` after clientInfo is known;
- close emits one `session_closed`;
- stateless compatibility request does not create a persistent connected session;
- each JSON-RPC request emits exactly one start and one finish/fail event;
- concurrent request contexts retain distinct `request_id` values;
- registered tool handler emits `tool_started` then exactly one `tool_finished`/`tool_failed` sharing the current request ID;
- telemetry delivery rejection does not alter the MCP response.

- [ ] **Step 2: Verify RED**

```bash
npm test -- --test-name-pattern="MCP traffic"
```

- [ ] **Step 3: Instrument `server/index.ts` request/session boundaries**

Parse initialize metadata only from the already-parsed JSON-RPC body; do not inspect prompt/tool payloads. Create a helper that extracts:

```ts
const method = typeof record.method === "string" ? record.method : null;
const clientInfo = method === "initialize" ? record.params?.clientInfo : undefined;
```

Measure request bytes from the raw decoded body buffer length where available and response bytes through a bounded response-byte counter/wrapper that does not retain body content. Exclude `/health`, OAuth, Workbench assets, and non-MCP routes entirely.

For stateful sessions, store normalized client metadata alongside `authIdentity`/`lastSeenAt` in `McpSessionRecord`. `transport.onclose`, pruning, and explicit close call `sessionClosed` idempotently.

Wrap `transport.handleRequest` in `mcpRequestContext.run(context, async () => ...)` and emit one terminal request event in `try/catch/finally` semantics.

- [ ] **Step 4: Instrument the existing `registerTool` wrapper in `server/mcp.ts`**

Extend `createMcpServer(..., options)` with optional telemetry. Immediately before `handler(...args)`, read `mcpRequestContext.getStore()` and emit `toolStarted(name, context)`. On success emit `toolFinished`; on error emit `toolFailed` with normalized code only.

Do not add a new ChatGPT-visible MCP tool and do not change tool schemas/annotations.

- [ ] **Step 5: Run complete plugin contract gates**

```bash
npm test
npm run typecheck
npm run build
```

Expected: all tests pass; MCP tool-count/schema tests unchanged.

- [ ] **Step 6: Commit Task 6 in plugin repo**

```bash
git add server/index.ts server/mcp.ts tests/mcp-traffic.test.ts
git commit -m "feat: instrument inbound MCP traffic"
```

---

### Task 7: Production-like two-client acceptance verifier

**Files:**
- Create in plugin repo: `scripts/check-mcp-traffic-integration.mjs`
- Modify in plugin repo: `package.json`
- Optionally create in computer repo: `tests/test_mcp_traffic_acceptance_contract.py` only for static contract assertions; do not make CI depend on an externally deployed plugin.

**Interfaces:**
- Adds plugin command `npm run check:mcp-traffic-integration`.
- Requires env-provided `CPTR_BASE_URL`, `CPTR_API_TOKEN`, and plugin MCP URL/token values; no host/port/token literals.

- [ ] **Step 1: Write the verifier to initialize two real MCP clients**

Use `@modelcontextprotocol/sdk` `Client` with names `ChatGPT Acceptance` and `Gemini Acceptance`. Connect both to the running plugin Streamable HTTP endpoint, call `tools/list`, then call one safe read-only tool such as `cptr_list_workspaces` from only one client.

The script then queries CPTR `/api/mcp/traffic/snapshot` with admin/test authorization suitable for the disposable local stack and opens `/api/mcp/traffic/stream` long enough to prove request/tool events arrive in order.

Assertions must include:

```js
assert.ok(snapshot.clients.some((client) => client.label === "ChatGPT"));
assert.ok(snapshot.clients.some((client) => client.label === "Gemini"));
assert.ok(events.some((event) => event.event_type === "request_started"));
assert.ok(events.some((event) => event.event_type === "tool_started"));
assert.ok(!JSON.stringify({ snapshot, events }).match(/authorization|bearer|arguments|result/i));
```

- [ ] **Step 2: Add failure-isolation acceptance**

Point telemetry delivery temporarily at a failing CPTR ingestion response or disable it through config, issue the same safe MCP call, and assert the MCP call still succeeds. Restore the normal endpoint before the final successful snapshot assertion.

- [ ] **Step 3: Run the verifier against a disposable local pair**

Start CPTR from the computer topology worktree and plugin from the telemetry worktree using environment variables rather than embedded addresses. Run:

```bash
npm run check:mcp-traffic-integration
```

Expected: exit 0 and printed confirmation for two clients, request/tool ordering, secret absence, session close, and failure isolation.

- [ ] **Step 4: Commit Task 7 in plugin repo**

```bash
git add scripts/check-mcp-traffic-integration.mjs package.json
git commit -m "test: verify MCP traffic topology integration"
```

---

### Task 8: Browser verification, full regression gates, docs, and coordinated PRs

**Files:**
- Modify computer spec/plan only if implementation facts require factual corrections; do not rewrite approved architecture without user review.
- Both Git roots: no unrelated files staged.

**Interfaces:**
- Produces two clean, pushed branches and two coordinated PRs against each repository's `main`.

- [ ] **Step 1: Run the complete computer backend/frontend gate**

From the computer topology worktree:

```bash
.venv/bin/python -m pytest tests/test_mcp_traffic.py tests/test_mcp_traffic_api.py -q
.venv/bin/python -m py_compile cptr/services/mcp_traffic.py cptr/routers/mcp.py
.venv/bin/ruff check cptr/services/mcp_traffic.py cptr/routers/mcp.py cptr/routers/gateway.py tests/test_mcp_traffic.py tests/test_mcp_traffic_api.py
.venv/bin/ruff format --check cptr/services/mcp_traffic.py cptr/routers/mcp.py cptr/routers/gateway.py tests/test_mcp_traffic.py tests/test_mcp_traffic_api.py
cd cptr/frontend
node --test tests/*.test.mjs
npx prettier --check src/lib/apis/mcp.ts src/lib/stores/mcp-traffic.ts src/lib/components/mcp/McpTopology.svelte src/lib/components/mcp/McpTopologyGraph.svelte src/lib/components/mcp/McpRecentRequests.svelte src/routes/mcp/+page.svelte tests/mcp-traffic-topology.test.mjs
npm run build
cd ../..
git diff --check
```

- [ ] **Step 2: Run the complete plugin gate**

From the clean plugin telemetry worktree:

```bash
npm test
npm run typecheck
npm run build
git diff --check
```

- [ ] **Step 3: Render and inspect desktop + mobile topology states**

Use CPTR managed Chrome against the local CPTR instance at desktop width around 1440×1000 and mobile width around 390×844. Inspect and capture evidence for:

- empty/no-telemetry state;
- ChatGPT + Gemini connected idle nodes around centered CPTR;
- active request edge/pulse;
- recent request row with In/Out + status;
- error flash state;
- telemetry reconnecting indicator;
- Console tab unchanged;
- mobile graph over recent-request sheet/list;
- reduced-motion static active-state behavior where browser emulation permits.

Do not bypass authentication or request/paste user credentials into chat. Use a disposable authenticated test session or local test auth fixture if needed.

- [ ] **Step 4: Rebase/refresh both branches on their latest remote mains if necessary**

Fetch each repository. If either `origin/main` advanced, rebase the clean feature branch, resolve only feature-related conflicts, and rerun the complete repository gate. Do not force-push without lease.

- [ ] **Step 5: Push both branches**

```bash
git push -u origin feature/mcp-traffic-topology
git push -u origin feature/mcp-traffic-telemetry
```

Run each command in its corresponding Git root.

- [ ] **Step 6: Open coordinated PRs**

Computer PR title:

```text
feat: add live MCP traffic topology
```

Plugin PR title:

```text
feat: emit MCP traffic telemetry for CPTR topology
```

Each PR description must cross-link the other PR, list its exact base/head SHA, state that no ChatGPT-visible MCP tools were added or removed, summarize privacy/bounds, and include the concrete test/build/browser evidence.

- [ ] **Step 7: Final acceptance report**

Report:

- both PR URLs;
- exact branch SHAs;
- exact test counts/results;
- production build/typecheck status;
- local two-client acceptance result;
- desktop/mobile visual verification result;
- any pre-existing warnings separately from feature regressions;
- deployment dependency: CPTR side can merge first, but real topology data requires the plugin telemetry PR to be merged/deployed too.
