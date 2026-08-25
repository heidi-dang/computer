# FlowDeck Phase 11 Checkpoints, Adaptive Routing, and Native UX Acceptance

Status: **IN PROGRESS — NOT ACCEPTED**

Phases 1–10 remain frozen. Phase 11 must not be marked accepted unless the
complete master-plan acceptance gate reaches at least 9.0/10 with zero open
P0/P1 defects and real desktop/mobile end-to-end evidence.

## Scope

- Durable checkpoint identity and fenced restore/reconciliation.
- Cancellation, restart, reconnect, UNKNOWN, and no-resurrection behavior.
- Adaptive routing that selects only existing native CPTR or FlowDeck paths.
- One native CPTR transcript across all verified FlowDeck surfaces.
- Final regression and master-plan closure audit.

## Safety invariants

- CPTR remains the sole model, browser, tool, and native execution authority.
- FlowDeck remains the sole durable orchestration authority.
- Adaptive routing never calls a model, switches providers, or creates a second
  execution loop.
- Checkpoint records are metadata/evidence only; restore requires an owned,
  fenced native operation.
- Unknown or ambiguous restore outcomes remain UNKNOWN/manual review.
- No unrestricted shell, MCP, FDX, deployment, publishing, GitHub push, DNS,
  credential rotation, or destructive external operation is enabled.

## Current evidence

- Phase 10 remains frozen at 9.2/10 with its acceptance record unchanged.
- Phase 11 checkpoint schema and adaptive policy are present, but checkpoint
  capture/restore routes, native transcript integration, desktop/mobile
  end-to-end qualification, and the final closure audit are still pending.

Phase 11 is intentionally not accepted.