# Capability OS Design A — Real UI Contract

## Goal

Redesign `/mcp → Capability OS` as a spatial operator dashboard while preserving CPTR's existing theme and making every runtime-looking state authoritative. The visual direction is Design A: orbital/spatial composition, compact telemetry, animated flows, progressive disclosure, and substantially less prose.

## Visual authority

- Preserve `app-theme` and existing semantic variables (`--app-bg`, `--app-surface`, `--app-surface-raised`, `--app-accent`, `--app-border`, foreground/muted/divider/hover tokens).
- Use token composition and `color-mix`; do not introduce a disconnected palette.
- Ambient orbit/particle motion is decorative only. Runtime pulses must derive from active runs, mounts, leases, or a connected stream.
- Support dark/light theme behavior, mobile layouts, keyboard focus, 44px touch targets, and `prefers-reduced-motion`.

## Feature-contract ledger

| UI element | Class | Backend authority | Transport/state | Failure / stale behavior | Verification |
|---|---|---|---|---|---|
| Task selector | interactive | `GET /api/mcp/capability-os/tasks` | REST, owner/admin scoped | retain current task when possible; empty state if none | API test + browser task switch |
| Selected task snapshot | live | `CapabilityOsOperatorService.snapshot` | new `GET /api/mcp/capability-os/stream?task_id=...` SSE | `connecting → live → reconnecting`; 404 emits terminal stream error | backend SSE test + frontend contract test |
| Manual refresh | interactive | tasks + operator REST snapshot | explicit REST refresh | error banner; existing snapshot retained if refresh fails | browser/network + source contract |
| Live badge | live | EventSource connection | `onopen`, `onerror`, stream snapshot | never claims live from timer alone | frontend contract + browser |
| Task core ring | derived | selected task `active/status/executionAllowed` | snapshot fields | dormant/blocked visuals when inactive/not allowed | frontend contract |
| Active run pulse | live | `views.taskCausality.activeRuns` | stream snapshot | no pulse when zero | frontend contract |
| MCP mount pulse | live | `views.mcpFabric.activeMounts` + `activeMounts` | stream snapshot | empty/offline rendered truthfully | frontend contract |
| Authority pulse | live | `views.authority.activeLeases` + `activeLeases` | stream snapshot + client clock for expiry text only | expiry countdown never changes lease authority | frontend contract |
| Sandbox runtime nodes | derived | `views.sandbox.runtime` | snapshot | unavailable nodes remain visibly unavailable | frontend contract |
| Forge activity | derived/live | `views.forge` | snapshot | failures visibly distinct | frontend contract |
| Evidence chain | derived | `views.taskCausality.evidenceChain` | snapshot | chain issue state if explicit false | frontend contract |
| Capability health distribution | derived | `artifactStates`, `views.capabilityHealth` | snapshot | empty inventory state | frontend contract |
| Evolution ring | derived | `views.evolution` | snapshot | zero-safe denominator | frontend contract |
| Release ladder | derived | `views.releases` | snapshot | zero-safe | frontend contract |
| Orbit navigation nodes | interactive/local | DOM section IDs | local scroll only | no backend claim | browser keyboard/click |
| Detail expanders | interactive/local | already-loaded snapshot | local disclosure | no backend mutation | browser keyboard/click |
| Ambient orbital animation | decorative | none | CSS only | disabled by reduced-motion | source + browser emulation |

## Explicit non-features

- No prompt/message box that claims to inject text into the current ChatGPT Official conversation. That transport does not exist.
- No buttons for Forge/Execute/Acquire mutation are added to this read-only operator projection.
- No fabricated progress percentages or synthetic operation histories.
- No fake "online" status derived from the 5-second client timer.

## Live-stream architecture

Add one owner/admin-scoped SSE endpoint for the selected Capability OS task. It emits an initial authoritative snapshot, then re-reads the bounded operator projection at a configurable interval and emits only when a stable JSON fingerprint changes. It emits keepalives when unchanged and a structured `capability_os_error` event when the task disappears. The frontend uses exactly one EventSource per selected task and closes it on task change/unmount.

The client clock remains only for relative timestamps and lease countdown labels. It must not drive health, connection, task, run, mount, or execution status.

## Acceptance

1. Backend stream test demonstrates RED then GREEN for initial snapshot, changed snapshot, keepalive/error semantics, and admin ownership.
2. Frontend contract test demonstrates RED then GREEN for EventSource helper, one selected-task stream, truthful live/reconnecting states, Design A structure, reduced-motion, and no fake prompt box.
3. `svelte-check` passes.
4. Production frontend build passes.
5. Relevant backend tests pass.
6. Managed browser verifies `/mcp` Capability OS on desktop and mobile when authentication/runtime permit it; otherwise the blocked visual gate is reported explicitly.
7. No new relevant browser console errors.
