# CPTR Live Workbench Product Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the Live Workbench product experience, preserve the 24-tool MCP contract, keep task artifacts organized, and expose clear progress and controls.

**Architecture:** Keep CPTR as the execution and runtime-state owner and the TypeScript repository as a thin MCP/UI adapter. Extend the bounded Workbench reducer and React view over the existing replayable stream; add only the MCP metadata needed to attach that view to direct execution. The already-implemented Python task-runtime layout remains the canonical artifact boundary.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy, unittest, TypeScript, MCP SDK, React 18, esbuild, Node test runner, CSS.

**Spec:** `docs/superpowers/specs/2026-08-26-cptr-workbench-product-features.md`

## Global Constraints

- Do not change OAuth, CPTR authorization, approval policy, worker cancellation, AWS deployment, or unrelated UI.
- Do not remove or rename any of the 24 MCP tools.
- Do not expose tokens, credentials, raw prompts, cookies, host paths, or unbounded worker output.
- Do not delete or migrate historical workspace data.
- Keep Live Workbench state bounded and replayable through the existing SSE cursor.
- Use task-owned runtime directories for transient agent, attachment, browser, and command-output state.
- Run TDD for each reducer/contract change before implementation.

---

### Task 1: Strengthen the Workbench state model

**Files:**
- Modify: `../chatgpt-computer-plugin/web/src/state.ts`
- Test: `../chatgpt-computer-plugin/tests/workbench.test.ts`

**Interfaces:**
- Consumes: `WorkbenchEvent` envelopes already emitted by CPTR.
- Produces: `WorkbenchState` fields `phase`, `activeOperation`, `workerTaskId`, `startedAt`, `lastEvent`, and `controlDelivery` for the React view.

- [ ] **Step 1: Write failing reducer tests** for phase derivation from `task.started`, `tool.started`, `verification.started`, terminal status, worker identity, and control delivery from `control.queued`/`control.consumed`.
- [ ] **Step 2: Run** `npm test -- --test-name-pattern='Workbench'` and confirm the new assertions fail because the state fields do not exist.
- [ ] **Step 3: Implement** bounded state derivation. Keep monotonic sequence rejection, cap every collection at 120 events, preserve the last event, and derive terminal state only from terminal events/statuses.
- [ ] **Step 4: Add tests** proving replayed events do not change phase or active operation and terminal state remains terminal.
- [ ] **Step 5: Run** `npm test -- --test-name-pattern='Workbench'` and confirm all Workbench tests pass.

### Task 2: Complete the Live Workbench presentation

**Files:**
- Modify: `../chatgpt-computer-plugin/web/src/workbench.tsx`
- Modify: `../chatgpt-computer-plugin/web/src/workbench.css`

**Interfaces:**
- Consumes: the extended `WorkbenchState` and existing `window.openai`/`ui/*` bridge.
- Produces: a responsive Live Workbench with progress rail, target summary, active operation, control delivery, tabbed event views, and terminal-safe controls.

- [ ] **Step 1: Add component-level test fixtures** in `tests/workbench.test.ts` for a running task, a verifying monitor, a consumed steering control, and a terminal task.
- [ ] **Step 2: Run** the focused test file and confirm the view-oriented state expectations fail.
- [ ] **Step 3: Implement** semantic UI regions: `header`, `progress`, `summary`, `controls`, `tabs`, `panel`, and `footer`. Render only bounded text from event payloads and show explicit empty/loading states.
- [ ] **Step 4: Add responsive CSS** for desktop and narrow viewports, preserving accessible focus states and `prefers-reduced-motion` behavior.
- [ ] **Step 5: Build** with `npm run build:web` and verify the bundle contains the new labels and no framework error text.

### Task 3: Attach the Workbench to direct execution and lock the 24-tool contract

**Files:**
- Modify: `../chatgpt-computer-plugin/server/mcp.ts`
- Test: `../chatgpt-computer-plugin/tests/mcp.test.ts`

**Interfaces:**
- Consumes: `ComputerClient.executeTask()` result containing `task_id`.
- Produces: `cptr_execute_task` result with the same target-bound `cptr/live` metadata as `cptr_start_task`, while preserving the existing structured result fields.

- [ ] **Step 1: Add a failing MCP test** asserting `cptr_execute_task` has the Workbench resource metadata and that all 24 tool names, safety annotations, and steering idempotency fields remain present.
- [ ] **Step 2: Run** `npm test -- --test-name-pattern='MCP|tool'` and confirm the execute-task metadata assertion fails.
- [ ] **Step 3: Implement** `cptr_execute_task` through `workbenchResult()` using its returned `task_id`; do not change its timeout or bounded-output behavior.
- [ ] **Step 4: Run** the focused MCP tests and then the complete plugin test suite.

### Task 4: Verify task-runtime organization and document the release contract

**Files:**
- Modify: `../computer/docs/control-plane.md`
- Test: `../computer/tests/test_task_runtime.py`

**Interfaces:**
- Consumes: existing `TASK_ROOT`, `task_runtime_dir()`, `ensure_task_runtime()`, and provider/command integration.
- Produces: regression coverage that every runtime category remains below the configured task root and is stable for the same task ID.

- [ ] **Step 1: Add a failing test** that checks all four category paths resolve below one task directory and that a second call does not create a sibling runtime.
- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_task_runtime -v` and confirm the new assertion fails if any integration regresses.
- [ ] **Step 3: Keep the existing implementation** as the source of truth; only add the smallest missing assertion or documentation needed.
- [ ] **Step 4: Run** the focused runtime tests and the complete CPTR suite.

### Task 5: Build and render-verify the product

**Files:**
- Modify: `../chatgpt-computer-plugin/README.md` if the tool/UI contract changed.

**Interfaces:**
- Consumes: the built Workbench bundle and local CPTR `/` endpoint.
- Produces: passing adapter tests, typecheck, build, and a rendered local Workbench smoke.

- [ ] **Step 1: Run** `npm test` and fix only failures caused by this feature.
- [ ] **Step 2: Run** `npm run typecheck` and `npm run build`.
- [ ] **Step 3: Run** `npm audit --omit=dev` and record the production result without changing unrelated dependencies.
- [ ] **Step 4: Run** `npm run dev` or use the existing local service bundle, open the Workbench resource in Browser/IAB, and verify page identity, non-blank content, console health, desktop viewport, mobile-sized viewport, and Stop/Steer state transitions with a disposable target.
- [ ] **Step 5: Run** the active CPTR health check and full Python suite; inspect both repositories’ diffs and confirm no credentials, runtime state, or fixture data are tracked.
