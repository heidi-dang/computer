# MCP Traffic Topology Design

## Goal

Add a live MCP traffic-topology subsystem to CPTR's merged `/mcp` console so operators can see the CPTR MCP server as the center node, connected MCP clients such as ChatGPT, Claude, Gemini, and other compatible clients around it, and real request activity pulsing from the active client into CPTR as MCP traffic occurs.

The visualization must be driven by real inbound MCP transport activity. It must not infer activity from the `/mcp` console's manual test calls and must not animate fabricated requests.

## Repository boundaries

The implementation spans two existing repositories because the UI and the inbound MCP transport live in different Git roots.

The `computer` repository owns the CPTR application, the merged `/mcp` console from PR #11, the new bounded telemetry buffer, snapshot/stream API, topology state reducer, graph rendering, recent-request table, and operator detail UI.

The `chatgpt-computer-plugin` repository owns the real ChatGPT-facing Streamable HTTP `/mcp` adapter. It is the only component that can authoritatively observe MCP initialize metadata, session lifecycle, JSON-RPC methods, registered tool calls, responses, failures, and request/response sizes for ChatGPT/Claude/Gemini traffic. It emits only normalized, sanitized telemetry to CPTR; it does not own topology UI state or durable analytics storage.

The repositories remain independent. The computer-side implementation branch is `feature/mcp-traffic-topology`, created from the latest `origin/main` containing merged PR #11 (`50389b9242d2c5d7c0dae70ddab99e4d9fcb7036`). The plugin-side implementation will use a clean branch from its latest `origin/main`, named `feature/mcp-traffic-telemetry`. Because real end-to-end telemetry requires changes in both Git roots, completion requires two coordinated PRs even though the operator-facing feature appears as one subsystem in CPTR.

## Existing integration points

PR #11 added `cptr/routers/mcp.py`, typed frontend wrappers in `cptr/frontend/src/lib/apis/mcp.ts`, and the `/mcp` console components. That console currently inspects CPTR-configured downstream MCP servers and logs tool invocations initiated from the console itself. Those calls are useful for server inspection but are not the inbound ChatGPT-to-CPTR request stream required by this feature.

The ChatGPT-facing adapter in `chatgpt-computer-plugin/server/index.ts` owns `StreamableHTTPServerTransport` sessions. It creates stateful MCP sessions, records their session IDs and authenticated identity, updates last-seen timestamps, and passes every MCP request through `transport.handleRequest`. `server/mcp.ts` already wraps `registerTool`, so tool-call start/result/error events can be instrumented at one registration boundary without editing every tool definition.

The plugin already has an authenticated `ComputerClient` used to call CPTR. The telemetry emitter will use that existing client/authentication boundary rather than introducing a second credential or hard-coded CPTR URL.

## Subsystem architecture

The subsystem has four isolated units:

1. **Plugin telemetry instrumentation** observes real inbound MCP transport and tool activity and emits sanitized events.
2. **CPTR telemetry store** accepts those events, maintains bounded current-session state and a bounded recent-event ring buffer, and fans new events out to subscribers.
3. **CPTR snapshot/SSE API** returns initial topology state and streams incremental events to an authenticated `/mcp` page.
4. **Topology frontend** hydrates from the snapshot, consumes SSE, derives client-node activity, and renders the graph plus recent-request/detail views.

The telemetry path is one-way from the plugin to CPTR. The visualization never reaches into plugin process memory, never depends on plugin log scraping, and never uses browser cross-origin access to the plugin.

## Client identity model

Each MCP session is assigned a normalized client identity when initialize metadata becomes available. Labeling priority is:

1. MCP `initialize.params.clientInfo.name` and optional version.
2. Known user-agent or transport metadata when clientInfo is absent.
3. The authenticated adapter identity when it is safe and useful as a fallback discriminator.
4. `Unknown MCP Client` when no trustworthy label exists.

Known display aliases normalize common names to stable labels such as `ChatGPT`, `Claude`, `Gemini`, `Codex`, and `MCP Inspector`. Unknown names are preserved as bounded sanitized text rather than being forced into a known brand.

Client identity is observational only. It is not used for authentication or authorization decisions.

## Telemetry event contract

The normalized event envelope is versioned and bounded:

```text
{
  version: 1,
  event_id: string,
  sequence: number,
  event_type: string,
  timestamp_ms: number,
  session_id: string | null,
  client: {
    id: string,
    label: string,
    version: string | null
  },
  request_id: string | null,
  method: string | null,
  tool_name: string | null,
  status: "started" | "complete" | "error" | "connected" | "disconnected",
  duration_ms: number | null,
  request_bytes: number | null,
  response_bytes: number | null,
  error_code: string | null
}
```

V1 event types are:

- `session_opened`
- `session_closed`
- `request_started`
- `request_finished`
- `request_failed`
- `tool_started`
- `tool_finished`
- `tool_failed`

The plugin generates stable per-process monotonically increasing sequences and opaque event/request IDs. CPTR assigns its own ingestion sequence for UI replay. Duplicate `event_id` values are ignored so retrying a telemetry delivery does not duplicate visual activity.

## What is intentionally not recorded

The subsystem does not store or stream:

- authorization headers, cookies, bearer tokens, API keys, or MCP session secrets;
- full request headers;
- raw tool arguments;
- raw tool results;
- prompt contents;
- filesystem paths extracted from tool arguments;
- chain-of-thought or model reasoning;
- arbitrary exception strings that may contain secrets or host paths.

Tool name, JSON-RPC method, status, timing, bounded client label/version, and byte counts are enough to drive the requested topology and recent-request UI.

Errors are normalized to a small public code set such as `timeout`, `validation_error`, `unauthorized`, `tool_error`, `transport_error`, and `internal_error`.

## Plugin instrumentation

### Session lifecycle

`server/index.ts` emits `session_opened` after a stateful MCP session is initialized and client identification is available. It emits `session_closed` when the transport closes, an idle session is pruned, or explicit cleanup closes the session.

Stateless compatibility requests do not create fake long-lived sessions. They receive an ephemeral client/session identity scoped to the request so request activity can still be shown without reporting a persistent connection.

### Request lifecycle

The adapter captures request metadata at the HTTP/MCP boundary before `transport.handleRequest` and emits `request_started` for relevant MCP JSON-RPC requests. Completion or failure emits exactly one terminal event with duration and byte counts when known.

The initial scope includes initialize, ping, tools/list, tools/call, resources/list, resources/read, prompts/list, prompts/get, and other MCP methods that pass through the adapter. HTTP health, OAuth, static assets, Workbench assets, and unrelated browser endpoints are excluded from the MCP topology stream.

### Tool lifecycle

The existing `registerTool` wrapper in `server/mcp.ts` emits `tool_started` immediately before an actual registered tool handler runs and `tool_finished` or `tool_failed` after it resolves. This gives the UI a precise active pulse for `tools/call` and a named tool even when the higher-level transport request is still running.

Request and tool events share a request correlation ID when possible. If SDK internals do not expose a safe correlation ID at the tool wrapper, the plugin uses an AsyncLocalStorage request context created at the request boundary; it does not rely on global mutable "current request" state.

### Delivery to CPTR

Telemetry delivery is asynchronous and best-effort. It uses a small in-process queue with a fixed maximum size and batches events to CPTR through the existing authenticated ComputerClient. MCP request completion must never wait on a slow topology consumer.

If the queue is full or CPTR telemetry ingestion is unavailable, the oldest unsent low-value activity can be dropped and a local dropped-event counter increments. Telemetry failure must not fail an MCP tool call or disconnect a client.

No package installation or external telemetry service is required.

## CPTR telemetry store

A new in-process `McpTrafficStore` owns topology telemetry for V1. It is deliberately non-durable.

It maintains:

- a bounded recent-event `deque`/ring buffer;
- a map of currently active sessions keyed by session ID;
- per-client aggregate counters for active sessions, requests, errors, last seen, and last active tool;
- an ingestion sequence;
- subscriber queues for SSE clients;
- a bounded set/LRU of recently seen event IDs for deduplication.

Default bounds come from configuration with conservative server-side fallbacks, for example recent events and subscriber queue sizes. No hostname, port, client label, model, token, or path is hard-coded.

On CPTR restart, the buffer starts empty and clients reconnect through fresh plugin events. Historical persistence is explicitly out of V1 scope.

## CPTR telemetry API

The existing MCP admin router gains a focused traffic namespace. Endpoints require the same admin authorization used by the merged `/mcp` console.

```text
POST /api/mcp/traffic/events
GET  /api/mcp/traffic/snapshot
GET  /api/mcp/traffic/stream
```

### Event ingestion

`POST /api/mcp/traffic/events` accepts a bounded batch of sanitized V1 envelopes from the plugin's existing CPTR credential. The endpoint validates schema and limits, deduplicates by `event_id`, updates session/client state, appends events, and publishes them to SSE subscribers.

The ingestion route must use an authentication path suitable for the plugin's bearer credential rather than relying only on browser cookie admin auth. It remains inaccessible without an authorized CPTR credential and is not a public anonymous webhook.

### Snapshot

`GET /api/mcp/traffic/snapshot` returns:

- CPTR center-node status;
- active client nodes and session counts;
- bounded recent events newest-last;
- current ingestion sequence;
- dropped-event/stream-health metadata when available.

No secrets or request payloads are returned.

### Stream

`GET /api/mcp/traffic/stream` uses SSE for authenticated browser clients. Events contain only the normalized telemetry envelope plus the CPTR ingestion sequence.

The stream sends periodic keepalive comments, supports disconnect cleanup, and bounds every subscriber queue. A slow browser may lose intermediate activity and must recover by requesting a new snapshot; it must never create unbounded server memory growth.

## Frontend topology state

The frontend adds a small pure reducer/store independent of the SVG renderer. It consumes snapshot state plus incremental telemetry and derives:

- current client nodes;
- active session counts;
- per-client active-request count;
- last activity timestamp;
- transient pulse IDs/state;
- recent request rows;
- selected client/request detail state.

Snapshot hydration and SSE event application are idempotent by ingestion sequence/event ID. Reconnecting SSE starts with a fresh snapshot rather than assuming no events were missed.

Activity visual state has bounded TTLs. A request/tool start lights the source client and connection edge immediately. Completion/error decays the highlight after a short UI-only delay so activity is visible without implying that the request is still running.

## `/mcp` UI integration

PR #11's console remains available. The page gains top-level view controls for `Topology` and `Console`, with Topology as the new live-traffic view and Console preserving the merged manual inspector/tool-call workflow.

### Desktop layout

The topology view uses two primary regions:

- a flexible graph canvas on the left/center;
- a fixed or bounded recent-requests panel on the right, visually similar to the provided reference image.

The CPTR MCP node is pinned at the center of the graph. Client nodes are distributed around it using deterministic radial slots derived from stable client IDs so nodes do not jump on every event.

Each client node shows a concise icon/initial, label, connection state, and optionally active-session count. CPTR shows overall active-client/request state.

### Mobile layout

On narrow screens, the graph occupies the upper region and the recent-request list becomes a lower sheet/list rather than forcing an unusable two-column canvas. Touch targets follow existing CPTR mobile sizing and NightOwl surface tokens.

### Connection and activity effects

Connected idle clients use a subdued edge and node state.

When a request starts, an animated directional pulse travels from the client node toward the center CPTR node. Concurrent requests can show repeated particles/pulses while the edge remains highlighted. The CPTR center node emits a short ring/ripple when requests arrive.

Successful completion returns the edge to idle after a short decay. Errors use the existing warning/error semantic colors briefly and then return to connected-idle state.

Animations respect `prefers-reduced-motion`. In reduced-motion mode, active state changes use static emphasis rather than moving particles.

The graph uses SVG/CSS and existing frontend dependencies. V1 does not introduce a graph-layout package, WebGL, or canvas dependency.

## Recent requests panel

The recent-request table is driven by request terminal events, with an in-progress row for active requests.

Columns are:

- Client
- Method / Tool
- In / Out
- Status
- When

`In / Out` means transport request bytes / response bytes when known. Unknown byte counts render as an em dash rather than an invented value.

Rows are bounded by the snapshot/event ring buffer. Clicking a row opens a safe detail view showing client, method, tool name, status, timing, byte counts, normalized error code, session identifier in shortened form, and timestamps. Raw arguments/results are not shown.

## Client detail interaction

Clicking a client node selects that client and shows:

- display label/version;
- connected session count;
- last seen;
- active request count;
- total requests in the current buffer;
- errors in the current buffer;
- recent method/tool names.

V1 filters are intentionally limited to client selection, active-only, and errors-only. Arbitrary historical time-range analytics are deferred until durable persistence exists.

## Error handling and degraded states

If the telemetry snapshot fails, the Topology view renders an explicit unavailable state while keeping PR #11's Console usable.

If SSE disconnects, the UI shows a reconnecting indicator, reconnects with bounded backoff, then rehydrates from a fresh snapshot. It does not silently claim the graph is live while disconnected.

If the plugin cannot deliver telemetry, MCP calls continue normally. CPTR may show `Telemetry delayed` or equivalent status based on last event/dropped-event metadata, but it must not mark MCP clients disconnected solely because no telemetry was delivered for a short period.

Session close events are authoritative when received. Stale sessions also expire server-side after a bounded inactivity timeout so an ungraceful plugin/client disconnect cannot leave a permanently connected node.

## Configuration

Configuration is environment/config driven. Proposed settings include:

```text
CPTR_MCP_TRAFFIC_ENABLED
CPTR_MCP_TRAFFIC_MAX_EVENTS
CPTR_MCP_TRAFFIC_MAX_SESSIONS
CPTR_MCP_TRAFFIC_SESSION_TTL_SECONDS
CPTR_MCP_TRAFFIC_SUBSCRIBER_QUEUE_SIZE
CPTR_MCP_TRAFFIC_PLUGIN_BATCH_SIZE
CPTR_MCP_TRAFFIC_PLUGIN_FLUSH_MS
```

Names may be aligned with existing configuration conventions during implementation, but the behavior and bounds above are required. Defaults must remain safe for the current single-process CPTR runtime.

## Security and privacy

The telemetry subsystem is operational observability, not request capture.

The plugin emits only the explicit allowlisted event fields. CPTR validates again rather than trusting arbitrary plugin-provided JSON. Browser snapshot/stream routes require admin access. Plugin ingestion requires an authenticated CPTR credential with the narrowest existing or newly defined scope that can support telemetry ingestion.

Client-provided labels are length-bounded and escaped by normal Svelte rendering. Session/request IDs are opaque. UI never displays the plugin's bearer token, CPTR token, OAuth tokens, cookies, raw headers, tool arguments/results, prompts, or filesystem paths.

## Performance characteristics

Instrumentation adds constant-time metadata work around requests and tool calls. Delivery is decoupled through a bounded best-effort queue. CPTR ingestion is append/update/fan-out over bounded structures.

The UI processes only the bounded snapshot and incremental SSE events. Graph layout is deterministic and linear in the number of visible clients. No request path performs database writes in V1.

The telemetry subsystem must not materially increase latency of MCP tool calls. A failing or slow topology path is always secondary to the primary MCP request path.

## Testing

### Computer repository

Backend tests cover:

- ingestion authorization;
- event schema bounds and rejection of unknown/oversized unsafe payloads;
- deduplication;
- session open/close and stale expiry;
- request/tool start/finish/error state transitions;
- bounded ring buffer and subscriber queues;
- snapshot shape and redaction;
- SSE replay/reconnect behavior where applicable.

Frontend tests cover:

- snapshot hydration;
- event reducer idempotency;
- client grouping and deterministic node layout;
- concurrent active request counts;
- pulse/decay state;
- reduced-motion behavior contract;
- recent-request table projection;
- disconnected/reconnecting states;
- responsive topology/console switching.

The existing PR #11 MCP console tests and global frontend regression/build gates must remain green.

### Plugin repository

Plugin tests cover:

- clientInfo normalization for ChatGPT, Claude, Gemini, known aliases, and unknown clients;
- stateful and stateless session lifecycle events;
- request start + exactly one terminal event;
- tool start + exactly one terminal event;
- correlation context across concurrent requests;
- byte-count calculation where available;
- allowlisted telemetry payloads with no argument/result/header leakage;
- batching, queue bounds, retry/drop behavior;
- telemetry failure not affecting MCP responses.

Existing MCP contract/tool-count tests must prove the telemetry implementation does not add or remove ChatGPT-visible MCP tools or alter the frozen tool schema contract.

## Runtime acceptance

A production-like acceptance test must start CPTR and the plugin locally with disposable credentials, initialize at least two synthetic MCP clients with different `clientInfo.name` values, issue `tools/list` and a safe tool call, and verify:

1. CPTR snapshot shows separate client nodes around the center CPTR MCP node.
2. A live SSE consumer receives request/tool start and terminal events in order.
3. The active client/edge state is raised only for the client making the request.
4. Request rows contain method/tool, status, duration, and byte counts when available.
5. Closing a client removes or idles the corresponding session state.
6. Telemetry contains no tool arguments/results or credentials.
7. Disabling or breaking telemetry delivery does not break the MCP request itself.

Browser verification must render the Topology and Console views at desktop and mobile widths and inspect active, idle, error, empty, and reconnecting states.

## Rollout and PR sequence

Implementation proceeds from clean latest-main worktrees in both repositories.

1. `computer`: `feature/mcp-traffic-topology` adds the telemetry store/API, frontend reducer/graph/recent-request UI, tests, and this design/implementation documentation.
2. `chatgpt-computer-plugin`: `feature/mcp-traffic-telemetry` adds transport/tool instrumentation and best-effort delivery through the existing ComputerClient, with adapter tests.
3. Verify both branches together in a production-like local stack.
4. Commit and push both clean branches.
5. Open coordinated PRs against each repository's `main`, cross-linking them and documenting the deployment dependency.

The computer PR may merge first because the topology UI handles an empty/unavailable telemetry stream, but the feature is not considered production-complete until the plugin telemetry PR is also merged and deployed.

## Out of scope for V1

- durable database persistence or historical analytics;
- billing/token accounting;
- packet/body capture;
- raw prompt/tool argument/result inspection;
- arbitrary user-defined graph layouts;
- downstream CPTR-to-third-party MCP topology in the same graph;
- WebGL/3D rendering;
- external observability vendors;
- host-controlled ChatGPT UI state beyond what the MCP adapter can observe.
