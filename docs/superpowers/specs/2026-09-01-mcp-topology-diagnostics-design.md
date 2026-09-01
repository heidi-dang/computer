# MCP Topology Diagnostics + Persistent Aliases Design

## Goal

Extend CPTR's existing `/mcp` operator surface into a truthful live topology and diagnostics console that:

- resolves the ChatGPT-facing adapter's unidentified fallback as `ChatGPT` instead of `Unknown MCP Client`;
- lets administrators rename any topology node with a server-persistent alias;
- shows live CPTR backend system metrics when the backend node is selected;
- displays measured/observed latency on topology edges without mislabeling processing time as network RTT;
- exposes safe, correlated request failures with enough structured context to diagnose bugs;
- preserves the existing separation between metadata-only topology telemetry and bounded/redacted tool Activity payloads;
- completes the existing frontend warning-cleanup acceptance before anything is released;
- integrates only verified changes into clean `main` branches and pushes `main` after all gates pass.

This design extends:

- `docs/superpowers/specs/2026-08-31-mcp-traffic-topology-design.md`
- `docs/superpowers/specs/2026-09-01-mcp-nightowl-live-activity-design.md`
- `docs/superpowers/specs/2026-09-01-frontend-build-warning-cleanup-design.md`

It does not replace their privacy, boundedness, or failure-isolation requirements.

## Scope and repository ownership

The work spans the existing `computer` and `chatgpt-computer-plugin` repositories.

### `computer`

Owns:

- persistent topology aliases;
- topology config API;
- bounded diagnostics/latency aggregation;
- live backend system-metrics collection;
- authenticated diagnostics snapshot/SSE APIs;
- topology node/edge selection UX;
- backend system-monitor panel;
- latency/error rendering in topology and Recent Requests;
- final frontend warning-cleanup integration and release verification.

### `chatgpt-computer-plugin`

Owns:

- the ChatGPT-facing MCP adapter fallback identity;
- authoritative request-boundary timestamps;
- adapter-to-backend latency measurement where both ends are under our control;
- safe structured request/tool failure metadata;
- correlation IDs linking topology Traffic and Activity;
- best-effort delivery that cannot fail the primary MCP request.

No new external monitoring service, graph framework, persistence service, or telemetry vendor is introduced.

## Canonical topology identity

The canonical infrastructure path remains:

```text
MCP client
   ↓
MCP Connector
   ↓
CPTR MCP
   ↓
CPTR Backend
```

Canonical node IDs are stable internal identities and never change when the display name changes.

Initial fixed IDs are:

```text
mcp-connector
cptr-mcp
cptr-backend
```

Known dynamic clients use their normalized telemetry IDs, including:

```text
chatgpt
claude
gemini
codex
mcp-inspector
```

Other clients retain their normalized bounded IDs.

### ChatGPT adapter fallback

The ChatGPT-specific adapter may currently fall back to `Unknown MCP Client` when neither `initialize.params.clientInfo` nor a useful User-Agent identifies the host.

That adapter-specific fallback will become:

```text
id: chatgpt
label: ChatGPT
```

This change is scoped to the known ChatGPT-facing MCP adapter/runtime. Generic or unrelated MCP adapters must retain truthful unknown-client behavior when their client cannot be identified.

Client identity remains observational and must never become an authorization input.

## Server-persistent aliases

Administrators can rename any topology node without changing its canonical identity.

### Persistence model

Aliases are stored in the existing instance `Config` model under a dedicated namespace, for example:

```text
mcp.topology.aliases
```

The stored value is a JSON object keyed by canonical node ID:

```text
{
  "chatgpt": "My ChatGPT",
  "mcp-connector": "Public MCP Gateway",
  "cptr-mcp": "CPTR Runtime",
  "cptr-backend": "Workstation Backend"
}
```

Requirements:

- aliases are bounded sanitized display strings;
- empty values remove the override;
- aliases persist across reloads, browsers, and CPTR restarts;
- aliases are shared by authenticated administrators;
- aliases never rewrite telemetry IDs, route names, credentials, request IDs, session IDs, or correlation IDs.

### API

The existing MCP admin router gains a topology-config namespace:

```text
GET /api/mcp/topology/config
PUT /api/mcp/topology/config
```

Both endpoints require administrator authorization.

The API returns canonical names and current aliases so the browser can always offer **Reset to default** without guessing.

### UI

Every selectable node detail view contains:

- display name;
- canonical node ID;
- editable alias;
- Save;
- Reset to default.

The graph always renders the current alias when present and otherwise renders the canonical label.

## Diagnostics architecture

A new bounded `McpDiagnosticsStore` lives alongside the existing `McpTrafficStore` and `McpActivityStore`.

It owns only operational diagnostics. It does not duplicate raw tool Activity payloads or topology session state.

The store maintains bounded windows for:

- per-hop latency samples;
- safe structured request failures;
- recent backend system-metric samples;
- latest health/status per infrastructure node/edge;
- subscriber queues and slow-subscriber/drop counters.

The store is intentionally bounded and operational. It is not a time-series database and does not retain unlimited history.

## Latency model

Latency labels must distinguish what is actually measured from what is derived.

### Client → MCP Connector

Without client cooperation, the server cannot truthfully measure ChatGPT's internet RTT.

The client-facing edge therefore shows **Observed request time** or **Request duration**, derived from timestamps visible at the adapter boundary. It must not be labeled `ping`, `RTT`, or `network latency` unless a future client protocol provides the required timestamps.

### MCP Connector → CPTR MCP

Where both timestamps are available inside the adapter/runtime boundary, record adapter processing/handoff duration as an observed server-side latency sample.

### CPTR MCP → CPTR Backend

Measure actual control-plane/backend API round-trip time around the backend request. This is a real server-side hop measurement and may be labeled **Backend RTT** or **Backend API latency**.

### Aggregation

For each measured/observed edge, keep a bounded rolling sample window and expose:

- latest;
- average;
- p50;
- p95;
- max;
- sample count;
- last updated timestamp;
- health classification.

Health thresholds are configurable or derived from conservative defaults; they must not be encoded as hidden UI-only constants.

The graph may display the latest compact value, for example `18 ms`, while the edge detail panel shows the full aggregation and the metric type so the operator knows exactly what the number means.

## Structured error diagnostics

The topology Traffic contract remains metadata-only, but request failures gain a safe structured diagnostic projection.

A failure record contains only bounded allowlisted fields such as:

```text
{
  version: 1,
  diagnostic_id: string,
  request_id: string | null,
  correlation_id: string | null,
  session_id: string | null,
  client_id: string,
  method: string | null,
  tool_name: string | null,
  stage: string,
  error_code: string,
  http_status: number | null,
  retryable: boolean | null,
  started_at_ms: number | null,
  completed_at_ms: number,
  duration_ms: number | null,
  request_bytes: number | null,
  response_bytes: number | null,
  summary: string
}
```

### Stages

Initial stage values are bounded enums describing where the failure occurred, for example:

```text
client_transport
mcp_connector
cptr_mcp
cptr_backend
activity_delivery
traffic_delivery
```

### Redaction

The diagnostic summary is sanitized using the existing redaction utilities before storage or transmission.

Diagnostics must never include:

- bearer tokens;
- API keys;
- authorization headers;
- cookies;
- OAuth tokens;
- raw request headers;
- unbounded exception strings;
- raw stack traces;
- private chain-of-thought/model reasoning;
- unbounded tool input/output;
- unredacted sensitive filesystem paths.

Raw bounded/redacted tool Input/Output/Error remains owned by the Activity channel and is linked by request/correlation ID.

### UI

When a request fails:

- the relevant node/edge gets an error state;
- Recent Requests shows `Error` plus the failing stage;
- selecting the request opens the structured diagnostic detail;
- the detail provides the matching correlation/request ID;
- when matching Activity exists, the UI can navigate to or reveal the corresponding Activity record.

If the system cannot determine a deeper root cause, the UI must say so rather than fabricate one.

## Correlation model

Traffic, Activity, latency, and diagnostics use the same request correlation context where possible.

The adapter creates or propagates:

- request ID;
- session ID;
- client identity;
- correlation ID.

The existing AsyncLocalStorage request context remains the mechanism for associating tool lifecycle events with the current transport request.

Correlation fields are opaque operational identifiers and never contain user payload content.

## Live backend system monitor

Selecting the `cptr-backend` node opens a live system-monitor detail panel.

### Metrics

The panel exposes bounded operational metrics when available:

- CPU utilization;
- CPU core count;
- load average;
- RAM used, available, and total;
- disk used, free, and total;
- disk read/write throughput;
- disk read/write operation rates when the OS exposes them;
- network receive/transmit throughput;
- GPU utilization;
- GPU memory used/total;
- GPU temperature when available;
- uptime;
- top CPU/memory processes;
- CPTR process health;
- MCP Traffic/Activity/Diagnostics subscriber health and drop counters.

### Collector design

Reuse the existing cross-platform system-info collection where practical, but separate static host identity from a new lightweight live sampler.

Requirements:

- expensive/blocking probes run off the asyncio event loop;
- samples are bounded to a short rolling window, approximately the latest minute at the selected sampling cadence;
- probe failure cannot affect MCP execution;
- unavailable metrics are represented as `Unavailable`, not zero;
- no package installation is required;
- Linux `/proc` and platform-native commands/APIs are preferred for CPU, memory, disk, network, and I/O;
- NVIDIA GPU data is collected only when supported tooling/API is already present;
- GPU absence or probe failure is a normal capability state, not an application error.

### API

The existing MCP admin router gains a diagnostics namespace:

```text
GET /api/mcp/diagnostics/snapshot
GET /api/mcp/diagnostics/stream
```

Both require administrator authorization.

The stream sends an initial bounded snapshot, incremental system/latency/error updates, keepalives, and cleans up subscriber state on disconnect.

## Topology interaction model

Every node and infrastructure edge is selectable.

### Client node detail

Shows:

- current alias/canonical name;
- version;
- connected sessions;
- active requests;
- request count;
- error count;
- last tool;
- last seen;
- observed request timing summary;
- recent correlated failures.

### MCP Connector detail

Shows:

- alias/canonical name;
- transport health;
- current client/session counts;
- adapter/handoff latency aggregation;
- recent connector-stage errors.

### CPTR MCP detail

Shows:

- alias/canonical name;
- MCP runtime health;
- current active request/tool state;
- handoff/backend latency summary;
- current telemetry subscriber/drop health;
- recent MCP-runtime errors.

### CPTR Backend detail

Shows:

- alias/canonical name;
- backend/control API health;
- backend RTT aggregation;
- full live system monitor;
- recent backend-stage errors.

### Edge rendering

Each infrastructure edge renders:

- latest timing value when available;
- metric type in its accessible/detail representation;
- healthy/degraded/error semantic state;
- last-updated age.

A missing measurement renders `—` or `Unavailable`; it never renders an invented zero.

## Frontend warning-cleanup release dependency

The existing warning-cleanup work is part of this release gate, not optional cleanup.

Before the topology diagnostics feature is considered releasable, the final frontend build must satisfy the existing warning-cleanup spec:

1. production build exits 0;
2. zero actionable Svelte compiler/accessibility warnings from `src/`;
3. zero ineffective dynamic-import warnings;
4. zero oversized initial/shared chunk warnings under the approved partitioning policy;
5. zero `PLUGIN_TIMINGS` diagnostics;
6. existing frontend regression suite remains green;
7. changed files pass Prettier and `git diff --check`;
8. `/mcp` desktop and 390×844 mobile smoke paths remain correct.

Warning suppression through blanket `svelte-ignore`, broad `onwarn`, or arbitrary multi-megabyte chunk limits is not acceptable.

## Testing strategy

Implementation follows TDD.

### Computer backend tests

Add focused tests for:

- alias persistence/read/reset and admin authorization;
- alias sanitization and bounded values;
- canonical IDs remaining unchanged after rename;
- latency rolling-window bounds and p50/p95/max calculations;
- structured diagnostics allowlist and redaction;
- unavailable GPU handling;
- disk/network/I/O sampling bounds;
- diagnostics SSE subscription cleanup and slow-subscriber behavior;
- correlation across Traffic/Activity/Diagnostics;
- system probes failing without affecting MCP request handling.

### Plugin tests

Add focused tests for:

- ChatGPT-facing unidentified fallback resolving to `ChatGPT`;
- unrelated unknown clients remaining unknown where appropriate;
- connector/backend timing capture;
- structured error stage/status/retryability projection;
- diagnostic redaction;
- correlation ID propagation;
- telemetry/diagnostics delivery failure isolation;
- unchanged MCP tool contract.

### Frontend tests

Add tests for:

- rendering persistent aliases;
- rename/save/reset flows;
- selection of clients, infrastructure nodes, and edges;
- correct latency labels by measurement type;
- backend monitor rendering CPU/RAM/disk/I/O/network/GPU unavailable/available states;
- structured error drill-down and Activity correlation;
- mobile detail behavior without horizontal overflow;
- NightOwl token use and keyboard accessibility.

### End-to-end acceptance

A real MCP SDK acceptance run must demonstrate:

1. a ChatGPT-named/fallback client connects;
2. Traffic remains metadata-only;
3. Activity contains bounded/redacted real tool input/output;
4. latency samples appear on the controlled server-side hops;
5. a deliberately induced safe test failure appears with the correct diagnostic stage and correlation ID;
6. backend system metrics stream without blocking MCP execution;
7. aliases survive a browser reload and CPTR config reload;
8. Activity/Traffic/Diagnostics delivery failures cannot change the underlying MCP tool result.

## Release and Git strategy

The primary `computer/main` checkout currently contains unrelated dirty work and must not be overwritten, reset, or accidentally committed.

Implementation and verification continue in clean isolated worktrees/branches.

The existing frontend warning-cleanup worker changes must be independently reviewed and integrated before final release acceptance.

After all Computer and plugin tests/build/browser gates pass:

1. fetch the latest remote main for each repository;
2. integrate/rebase the verified feature history onto a clean main-derived worktree;
3. resolve only genuine feature conflicts without overwriting unrelated work;
4. rerun the complete final gate on the exact candidate commits;
5. commit any remaining reviewed integration changes;
6. push the verified `main` branch for each affected repository.

No deployment is implied by the `main` push unless separately requested.

## Non-goals

This change does not:

- build a general-purpose Prometheus/Grafana replacement;
- persist long-term system-metric history;
- claim to measure ChatGPT internet RTT without client-side timing support;
- expose raw secrets, headers, stack traces, tool payloads, or chain-of-thought in topology diagnostics;
- make aliases authorization-sensitive;
- make telemetry delivery part of the MCP request success path;
- install monitoring packages solely for this feature;
- rewrite unrelated dirty work in the primary checkout.

## Acceptance definition

The feature is complete only when all of the following are true:

- ChatGPT-facing unidentified traffic appears as `ChatGPT` rather than `Unknown MCP Client`;
- every topology node can be renamed and reset, with aliases persisted server-side;
- clicking CPTR Backend shows live bounded CPU/RAM/disk/I/O/network/GPU-capability/system health data;
- topology edges show truthful measured/observed latency with aggregation;
- failures expose safe structured stage/correlation diagnostics and link to matching Activity where available;
- Traffic remains metadata-only and Activity remains bounded/redacted;
- backend/plugin/frontend tests pass;
- real MCP two-channel/diagnostics acceptance passes;
- desktop and 390×844 mobile verification pass;
- the frontend production build satisfies the zero-actionable-warning gate;
- both affected repositories are clean at the final candidate revisions;
- the verified changes are committed and pushed to `main` without overwriting unrelated local work.
