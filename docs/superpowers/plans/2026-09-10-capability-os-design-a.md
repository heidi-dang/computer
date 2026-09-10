# Capability OS Design A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the text-heavy `/mcp → Capability OS` operator view with the approved Design A spatial dashboard, backed by a real owner-scoped live snapshot stream and existing CPTR theme tokens.

**Architecture:** Keep Capability OS operator semantics read-only. Add one authenticated SSE endpoint for selected-task snapshots, one typed frontend EventSource helper, and redesign the existing Svelte component around a central task core plus orbit/navigation nodes and compact visual telemetry. Runtime-looking state is driven only by server snapshot fields or EventSource connection state; ambient CSS motion is decorative and reduced-motion safe.

**Tech Stack:** FastAPI, asyncio, Python, Svelte 5, TypeScript, native EventSource, CSS token composition, Node test runner, pytest.

**Spec:** `docs/superpowers/specs/2026-09-10-capability-os-design-a.md`

## Global Constraints

- Preserve existing backend authority and admin ownership.
- No mutation controls are introduced in the read-only Capability OS operator UI.
- No fake ChatGPT prompt injection.
- No fake live/progress state.
- Exactly one Capability OS EventSource per selected task.
- Preserve existing CPTR theme tokens and mobile 44px touch targets.
- `prefers-reduced-motion` removes nonessential animation.
- No new frontend or backend dependencies.

---

### Task 1: Add authoritative Capability OS SSE

**Files:**
- Modify: `cptr/routers/mcp.py`
- Test: `tests/test_mcp_capability_os_api.py`

**Interfaces:**
- Produces: `GET /api/mcp/capability-os/stream?task_id=<id>&limit=<n>`.
- Emits: `snapshot` and `capability_os_error` SSE events plus keepalive comments.
- Uses existing `CapabilityOsOperatorService.snapshot(user_id, task_id, limit)`.

- [ ] Write a failing API test for the missing stream function and expected initial `snapshot` event.
- [ ] Run it and confirm RED is due to the missing stream endpoint/function.
- [ ] Implement the smallest owner/admin-scoped stream with stable JSON fingerprint change detection and bounded interval/keepalive settings.
- [ ] Add changed-snapshot and missing-task error assertions.
- [ ] Run the focused backend test to GREEN.

### Task 2: Add typed frontend stream contract

**Files:**
- Modify: `cptr/frontend/src/lib/apis/mcp.ts`
- Create: `cptr/frontend/tests/mcp-capability-os.test.mjs`

**Interfaces:**
- Produces: `McpCapabilityOsStreamCallbacks` and `openMcpCapabilityOsStream(taskId, callbacks, limit?)`.
- Uses exactly one native `EventSource` and parses `snapshot` / `capability_os_error`.

- [ ] Write failing source/compile tests for the new EventSource helper and selected-task live-state contract.
- [ ] Run Node test and confirm RED.
- [ ] Implement the typed helper with cleanup closure.
- [ ] Re-run Node test to GREEN.

### Task 3: Implement Design A spatial operator dashboard

**Files:**
- Modify: `cptr/frontend/src/lib/components/mcp/McpCapabilityOs.svelte`
- Test: `cptr/frontend/tests/mcp-capability-os.test.mjs`

**Interfaces:**
- Consumes existing `McpCapabilityOsOperatorSnapshot` plus the new stream helper.
- Produces a central task core, real-state pulses, orbit/navigation nodes, compact health/runtime/evidence/evolution/release surfaces, progressive detail, manual refresh, loading/error/empty/reconnecting states.

- [ ] Extend the frontend test with Design A structure, no fake prompt input, runtime-driven pulse rules, accessibility, responsive and reduced-motion assertions; confirm RED.
- [ ] Replace timer-driven snapshot polling with selected-task SSE; retain low-frequency task-list refresh and explicit manual REST refresh.
- [ ] Implement spatial hero/core and orbit navigation using existing semantic tokens.
- [ ] Replace explanatory paragraphs with compact data visuals and disclosure while preserving all current operator data.
- [ ] Add connection state (`connecting/live/reconnecting`) and stale-age label driven by last server snapshot receipt.
- [ ] Add reduced-motion and mobile rules.
- [ ] Run compile/contract test to GREEN.

### Task 4: Verification and delivery

**Files:**
- All changed files.

- [ ] Run focused backend Capability OS API tests.
- [ ] Run existing relevant Capability OS backend tests.
- [ ] Run frontend Node contract tests.
- [ ] Run `npm --prefix cptr/frontend run check`.
- [ ] Run `npm --prefix cptr/frontend run build`.
- [ ] Start/reuse a development runtime if available and inspect `/mcp` through managed Chrome at desktop and mobile widths; inspect console and network.
- [ ] Review diff for hardcoded unrelated colors, fake runtime state, excessive prose, unbounded animation/listeners, and stale EventSource cleanup.
- [ ] Commit and push the implementation branch and open/update a PR. Do not merge or deploy without explicit authorization.
