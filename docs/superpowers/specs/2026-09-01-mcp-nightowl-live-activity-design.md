# MCP NightOwl + Live Activity Design

## Goal

Finish the `/mcp` operator experience so it matches CPTR's current appearance system, behaves correctly on mobile, represents the real request path, and shows authoritative live ChatGPT MCP tool activity including bounded redacted input and output.

This design extends, but does not replace, the existing MCP Traffic Topology subsystem defined in `docs/superpowers/specs/2026-08-31-mcp-traffic-topology-design.md`.

The topology traffic channel remains metadata-only. A separate MCP Activity channel carries bounded redacted tool input/output for the admin Console.

## Scope

The implementation spans the existing `computer` and `chatgpt-computer-plugin` repositories.

The `computer` repository owns:

- `/mcp` NightOwl/theme integration;
- Back-to-CPTR-home navigation;
- compact responsive Recent Requests UI;
- fixed infrastructure nodes for the request path;
- the bounded MCP Activity store;
- authenticated activity ingestion/snapshot/SSE endpoints;
- the Console Activity feed and downstream-server inspector controls.

The `chatgpt-computer-plugin` repository owns:

- authoritative observation of every ChatGPT-visible registered MCP action;
- the existing redaction/bounding of arguments, results, and errors;
- best-effort delivery of those activity records to CPTR;
- correlation between request/tool lifecycle and client/session metadata when available.

No new third-party service, database, graph package, or browser-to-plugin cross-origin dependency is introduced.

## Existing behavior and problems

### Theme mismatch

The current `/mcp` components still contain many explicit `bg-white`, `bg-gray-*`, and `dark:*` utility combinations. CPTR already exposes appearance tokens such as `--app-bg`, `--app-surface`, `--app-surface-raised`, `--app-border`, `--app-fg`, and `--app-accent`. In NightOwl these resolve from the canonical palette centered on `#011627`, `#d6deeb`, and `#82aaff`.

The MCP page must use those tokens directly so NightOwl, light mode, system mode, and custom appearance colors all remain coherent.

### Mobile recent-request overflow

`McpRecentRequests.svelte` currently renders a desktop table with `min-w-[39rem]`, forcing horizontal overflow on a narrow phone viewport.

### Ambiguous topology semantics

The current topology renders only client nodes around CPTR MCP. An unidentified client can visually look like an infrastructure hop even though `Unknown MCP Client` means only that client metadata was unavailable.

### Console activity is manual-only

`McpConsole.svelte` owns a local `callLog` populated only by tool invocations initiated from the `/mcp` Console. Real ChatGPT MCP tool calls are not shown there.

The plugin already wraps every `registerTool()` handler in `server/mcp.ts` and publishes prompt-session activity with:

- `STARTED` plus bounded/redacted arguments;
- `COMPLETE` plus bounded/redacted result JSON;
- `FAILED` plus a bounded/redacted error envelope.

That capture point is authoritative and must feed the Console Activity view.

## Request-path topology

The graph will separate dynamic clients from fixed infrastructure.

The logical path is:

```text
ChatGPT / Claude / Gemini / Unknown Client
                ↓
          MCP Connector
                ↓
             CPTR MCP
                ↓
          CPTR Backend
```

### Dynamic client nodes

Dynamic nodes continue to come from the existing metadata-only traffic snapshot/reducer.

Known clients normalize to stable labels such as ChatGPT, Claude, Gemini, Codex, and MCP Inspector. Unidentified clients remain `Unknown MCP Client`.

### Fixed infrastructure nodes

The graph adds three fixed infrastructure roles:

1. **MCP Connector** — the public/client-facing MCP adapter and transport boundary.
2. **CPTR MCP** — the MCP execution/server layer.
3. **CPTR Backend** — the CPTR control/backend destination.

These are presentation semantics, not synthetic telemetry sources. They do not create fake sessions or request counts.

### Animation

A real request/tool activity pulse enters from the relevant dynamic client and travels through MCP Connector → CPTR MCP → CPTR Backend.

If client identity is unavailable, the pulse starts at `Unknown MCP Client`.

Success returns the path to idle after a short visual decay. Failures briefly use the error semantic color. `prefers-reduced-motion` replaces moving particles with static active emphasis.

## Theme integration

The `/mcp` page will use CPTR appearance tokens instead of page-specific light/dark palettes.

Primary tokens:

- `--app-bg`
- `--app-fg`
- `--app-surface`
- `--app-surface-raised`
- `--app-surface-subtle`
- `--app-fg-muted`
- `--app-fg-subtle`
- `--app-border`
- `--app-divider`
- `--app-hover`
- `--app-active`
- `--app-accent`
- `--app-accent-strong`
- `--app-accent-soft`
- `--app-focus-ring`
- `--app-shadow-color`

The route shell, Topology/Console tabs, summary card, topology canvas, Console sections, server/tool list, inputs, output cards, selected rows, recent requests, and detail surfaces will all derive from these tokens.

NightOwl must therefore render as deep navy surfaces with cool blue focus accents without hard-coding NightOwl-only CSS. Custom user appearance settings remain authoritative.

## Header and navigation

The `/mcp` header adds a touch-friendly Back control before the MCP title.

Behavior:

- Back navigates to `/`, CPTR Home.
- The control has a minimum 44 px mobile target.
- Topology/Console remains available in the same header.
- On narrow viewports the header stays compact without clipping the title or tabs.

## Recent Requests responsive design

### Mobile

Below the topology canvas, Recent Requests becomes a compact list rather than a wide table.

Each row uses two primary lines:

```text
Client label                         status · when
Tool name or MCP method              request bytes / response bytes
```

Optional client version is rendered as subdued supporting text when present.

A tap opens the existing safe request detail view. The mobile list must not impose a desktop minimum width or require horizontal scrolling.

### Desktop

Desktop keeps the full table with:

- Client
- Method / Tool
- In / Out
- Status
- When

The desktop and mobile views consume the same bounded `McpRecentRequestRow` projection.

## Separate MCP Activity channel

### Privacy boundary

The existing `/api/mcp/traffic/*` contract remains metadata-only and continues to exclude raw tool arguments/results.

Tool input/output is carried only by a new `/api/mcp/activity/*` namespace intended for authenticated CPTR admin/operator use.

This separation prevents graph telemetry consumers from accidentally gaining execution payload access.

### Activity event contract

The plugin emits a versioned bounded activity envelope:

```text
{
  version: 1,
  event_id: string,
  sequence: number,
  timestamp_ms: number,
  client: {
    id: string,
    label: string,
    version: string | null
  },
  session_id: string | null,
  request_id: string | null,
  tool_name: string,
  title: string | null,
  phase: "started" | "complete" | "failed",
  summary: string,
  arguments_json: string | null,
  result_json: string | null,
  error_json: string | null,
  duration_ms: number | null
}
```

CPTR assigns its own ingestion sequence for replay.

### Redaction and size limits

The plugin's existing `terminalJson`, `terminalToolResult`, error envelope, and terminal redaction helpers remain the source of payload sanitization.

The Activity channel must never include:

- authorization headers;
- cookies;
- bearer tokens or API keys;
- private chain-of-thought/model reasoning;
- unbounded binary/base64 payloads;
- arbitrary raw HTTP headers;
- unbounded stdout/stderr or files;
- unredacted host paths when existing redaction rules remove them.

Every string field and each JSON payload is server-bounded. Oversized output is truncated with an explicit marker rather than growing memory without limit.

### Best-effort delivery

Activity delivery must not block or fail the primary MCP tool call.

The plugin uses a bounded in-memory queue/batcher similar to the existing traffic emitter. If CPTR activity ingestion is unavailable, MCP tool execution still succeeds/fails according to the underlying tool result.

Queue overflow increments a local dropped-event counter and drops bounded old activity rather than blocking tool execution.

## Plugin instrumentation

The existing one-time `registerTool` wrapper remains the only tool-instrumentation boundary.

For every real ChatGPT-visible MCP tool invocation:

1. derive client/request/session context from the existing `mcpRequestContext` when available;
2. emit `started` using the already-redacted argument JSON;
3. run the actual handler;
4. emit `complete` with the already-redacted bounded terminal result and elapsed time; or
5. emit `failed` with the already-normalized/redacted error envelope and elapsed time.

No tool definition is individually instrumented. The ChatGPT-visible MCP tool count, names, schemas, annotations, and behavior remain unchanged.

Worker-scoped direct-coding tools may continue to publish their richer existing Workbench `direct.worker` records, but they must also emit one normalized Activity record so the `/mcp` Activity feed can represent every MCP tool call consistently.

## CPTR Activity store

A new in-process bounded `McpActivityStore` owns V1 Console activity.

It maintains:

- a bounded recent-event ring buffer;
- a bounded dedupe set/LRU keyed by `event_id`;
- CPTR ingestion sequence;
- bounded subscriber queues;
- slow-subscriber/drop counters.

The store is intentionally non-durable in V1. A CPTR restart clears the feed.

No topology/session aggregates are duplicated here; those remain owned by `McpTrafficStore`.

## CPTR Activity API

The existing MCP router gains:

```text
POST /api/mcp/activity/events
GET  /api/mcp/activity/snapshot
GET  /api/mcp/activity/stream
```

### Ingestion

`POST /api/mcp/activity/events` requires a dedicated plugin bearer scope, `mcp:activity:write`.

It validates the strict bounded activity schema, deduplicates events, appends accepted records, and fans them out to SSE subscribers.

### Snapshot and stream

`GET /api/mcp/activity/snapshot` and `GET /api/mcp/activity/stream` require the same admin browser authorization as the rest of `/mcp`.

The SSE stream sends an initial snapshot, incremental activity events, keepalive comments, and cleans up subscriber state on disconnect.

Slow clients may lose intermediate events and must recover from a new snapshot; they must never create unbounded memory growth.

## Console Activity UX

The mobile Console section navigation remains:

```text
Servers | Activity | Tool
```

Desktop retains its three-pane inspector layout.

### Activity feed

Activity is no longer limited to local manual calls. It hydrates from `/api/mcp/activity/snapshot` and stays current through `/api/mcp/activity/stream`.

Every record displays:

- client label/version when known;
- tool name/title;
- running/success/error state;
- start/completion time;
- elapsed time;
- bounded redacted Input;
- bounded redacted Output or Error.

New events append live and the panel follows the newest event unless the operator has intentionally scrolled away from the bottom.

A Clear control clears only the browser's current presentation/filter state; it does not mutate or erase server activity history. Refresh/resnapshot restores the server-bounded recent feed.

### Manual Console invocations

Tool invocations initiated from `/mcp` continue to use the real downstream MCP endpoint and streaming response path.

They must appear in the unified Activity presentation without creating a duplicate second local-only record. If a manual downstream server invocation cannot be represented by the plugin-origin Activity channel because it does not traverse the ChatGPT-facing plugin, the Console may render a clearly marked local `Console invocation` record alongside authoritative plugin activity, with dedupe/correlation when IDs are available.

The UI must never label a manual downstream test call as ChatGPT activity unless it actually originated from the ChatGPT-facing plugin.

## Console function audit

The repair includes functional verification of all existing Console operations:

- server list load;
- server health display;
- server expand/collapse;
- tool list load;
- refresh server list;
- reconnect failed server;
- tool selection;
- generated parameter form;
- enum and boolean inputs;
- number/integer inputs;
- object/array JSON inputs;
- raw JSON mode toggle;
- invalid JSON validation;
- Invoke button;
- Ctrl/Cmd+Enter submit;
- streaming `tool_chunk` handling;
- `tool_done` result rendering;
- `tool_error` rendering;
- image/resource/text result rendering;
- request expansion/collapse;
- mobile Servers/Activity/Tool section switching;
- browser Activity resnapshot/reconnect;
- Clear presentation behavior;
- Admin links.

Failures must surface bounded operator-facing errors; buttons must not silently fail.

## Error handling

### Activity producer

Activity-delivery failure never changes the actual MCP tool result.

### CPTR ingestion

Malformed payloads fail validation. Missing bearer authentication returns 401 and missing `mcp:activity:write` scope returns 403.

### Browser stream

SSE loss changes Activity state to reconnecting and uses bounded backoff. Reconnect obtains a new snapshot before continuing incremental events.

### Downstream MCP Console actions

Existing 4xx/5xx/timeout behavior remains, but every actionable failure must be rendered in Activity or surfaced by toast/error state rather than leaving the UI apparently idle.

## Repository and branch strategy

The current Computer repair branch is `fix/mcp-mobile-runtime`, based on `computer/origin/main` at `ba3a891be7e68120791c3ad14516f2b09fdc58a3` when this design is written. It already contains uncommitted mobile Console fixes that must be preserved.

Plugin implementation must occur on a clean feature branch/worktree from the latest `chatgpt-computer-plugin/origin/main` at implementation time. Existing dirty development or production checkouts must not be overwritten.

Because the live feature requires coordinated source changes in both repositories, final delivery requires coordinated commits/PRs or explicitly approved direct merges in both Git roots.

## Testing strategy

### Computer backend

Add tests for:

- strict activity schema and unknown-field rejection;
- field/payload size limits;
- dedupe and ring-buffer bounds;
- subscriber queue bounds and cleanup;
- `mcp:activity:write` authentication;
- admin-only snapshot/SSE;
- SSE initial snapshot and incremental replay;
- malformed event rejection;
- separation between `/traffic/*` metadata and `/activity/*` payload-bearing contracts.

### Plugin

Add tests for:

- every registered MCP action producing started + terminal Activity records;
- argument/result/error redaction and bounds;
- worker-scoped tools also producing normalized Activity;
- request/client/session correlation;
- queue bounds;
- delivery retry/drop behavior;
- activity delivery failure isolation;
- unchanged ChatGPT-visible tool contract.

### Frontend

Add source/behavioral tests for:

- use of `--app-*` tokenized surfaces instead of MCP-specific white/gray page backgrounds;
- Back button to `/`;
- fixed MCP Connector and CPTR Backend graph nodes;
- dynamic unknown client remains a client, not infrastructure;
- mobile Recent Requests has no desktop minimum-width overflow;
- desktop table remains present;
- Activity snapshot + SSE reducer behavior;
- running/complete/failed presentation;
- bounded redacted Input/Output rendering;
- mobile Console section navigation;
- all Console controls listed in the audit.

### Runtime acceptance

Acceptance requires:

1. full Computer backend tests;
2. full MCP frontend regressions;
3. Computer production build;
4. full plugin tests/typecheck/build;
5. a real fresh MCP SDK session identified as ChatGPT invoking a real tool;
6. evidence that topology shows the client path and Activity shows the same real tool's started + completed output records;
7. telemetry/activity delivery failure isolation;
8. 390×844 NightOwl browser verification;
9. desktop browser verification;
10. confirmation that existing non-MCP CPTR appearance and navigation remain unaffected.

## Acceptance criteria

The work is complete only when all of the following are true:

- `/mcp` uses the active CPTR appearance tokens and visually matches NightOwl when NightOwl is active.
- The page has a reliable Back control to CPTR Home.
- Mobile Recent Requests fits the viewport without horizontal table overflow.
- The graph distinguishes dynamic MCP clients from MCP Connector, CPTR MCP, and CPTR Backend infrastructure.
- Real request pulses follow client → MCP Connector → CPTR MCP → CPTR Backend.
- `Unknown MCP Client` is used only for genuinely unidentified clients.
- Console Servers, Activity, and Tool functions work on mobile and desktop.
- Activity shows every real registered ChatGPT MCP tool call observed by the plugin, with bounded redacted input and output/error.
- Topology traffic remains metadata-only and never gains raw arguments/results.
- Activity delivery failure does not fail MCP tools.
- Both repositories pass their complete relevant test/type/build gates.
- Real end-to-end runtime acceptance passes before deployment is called finished.
