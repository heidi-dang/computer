# Capability OS Live UI Audit — 2026-09-10

## Scope

Re-audit `/mcp → Capability OS` against the workspace-managed `designing-real-ui` skill, with emphasis on truthful ChatGPT activity visibility and backend/UI contract consistency.

## Finding

Design A correctly used the owner-scoped Capability OS SSE for task snapshots, but the snapshot exposed only aggregate task state. The six public Capability OS operations — Inspect, Resolve, Forge, Execute, Acquire, and Reflect — were not projected as task-scoped live actions. A ChatGPT Capability OS call could therefore complete without a visible action transition unless an aggregate counter happened to change.

## Root cause

The plugin already propagated bounded correlation metadata (`X-CPTR-Trace-Id`, request ID, MCP session ID, tool name) into the Capability OS Control API. The backend Action Trace store already correlated ChatGPT/MCP/backend layers, but Capability OS router stages were not tagged with the owning task and the operator projection did not consume task-scoped trace summaries.

## Fix

- Add task identity to the bounded Action Trace metadata model and task-scoped summary filtering.
- Record `started → ok/error` metadata stages around all six Capability OS router operations using the existing plugin trace context.
- Keep observability fail-open relative to execution: trace recording errors never alter Capability OS execution results.
- Project at most 10 recent task-scoped Capability OS actions into the existing operator snapshot; no prompts, inputs, outputs, claims, or payloads are included.
- Reuse the existing selected-task Capability OS SSE. No second stream or polling loop is added.
- Add a compact `ChatGPT → Capability OS` live-action rail to Design A. Active operations animate only while backend lifecycle state is `started/running`; completed and failed operations retain bounded recent history.
- Preserve existing CPTR semantic theme tokens, responsive behavior, keyboard semantics, and reduced-motion handling.

## Contract

UI action cards are authoritative projections of backend trace metadata:

`ChatGPT MCP request → plugin correlation headers → Capability OS router lifecycle stage → task-scoped ActionTraceStore → CapabilityOsOperatorService snapshot → existing Capability OS SSE → Design A live-action rail`

Displayed fields are limited to operation/suboperation, lifecycle status, source classification, tool name, timestamps/duration, sanitized error code, and trace layers.

## Verification

- Plugin trace/capability routing tests: 48/48 passed.
- Capability OS + Action Trace backend suite: 222 passed, 2 skipped.
- `/mcp` frontend contracts: 61/61 passed.
- Capability OS focused UI contracts: 5/5 passed.
- Svelte diagnostics: 0 errors, 0 warnings.
- Ruff: passed.
- Production frontend build: passed.
- Capability OS CSS chunk: 40.45 kB, 6.75 kB gzip; no new dependency.
- Real browser visual QA remains blocked because the registered Heidi Chrome device is currently disconnected; isolated Chrome does not share authenticated application state.

## Audit result

Functional/backend consistency: PASS.
Live Capability OS action visibility: PASS in implementation and automated integration evidence.
Theme/accessibility/reduced-motion contract: PASS.
Authenticated rendered visual inspection: PENDING due unavailable connected user Chrome session.
Deployment: NOT performed by this audit; merge/deploy require separate authorization.
