# Control Plane Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the production gaps in the pushed CPTR control-plane branches so autonomous execution shares the real CPTR lifecycle, independently verifies work, persists approvals/evidence, recovers after restart without duplicate tasks, and demonstrates a deterministic reject-repair-complete cycle.

**Architecture:** Keep `computer` as the execution and supervision authority. Extend the existing `AgentService` boundary so gateway and Control API both invoke the same `chat_task` engine, add injected independent verification and durable evidence/approval stores to the supervisor, and use workspace leases plus persisted idempotency to coordinate recovery. Keep the plugin thin: add MCP forwarding for persistent monitor status, evidence, steering, cancellation, and approval while CPTR enforces policy.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy/Alembic, SQLite, asyncio, existing CPTR `chat_task`, unittest, TypeScript, `@modelcontextprotocol/sdk`, Zod, Streamable HTTP.

**Spec:** `docs/superpowers/specs/2026-08-23-chatgpt-control-plane-design.md` and the user-provided production-hardening override.

## Global Constraints

- Continue from the existing pushed feature branches; do not recreate repositories or branches.
- Remove all obsolete hosting-project material before feature work; do not touch unrelated account-level resources.
- Do not modify `computer/main`, merge branches, force-push, or deploy publicly.
- Preserve existing CPTR UI and `/v1/chat/completions` behavior.
- Do not trust worker prose as proof of completion; every scope must pass independent evidence checks and a final gate.
- Do not expose chain-of-thought, unrestricted credentials, raw filesystem paths as identity, or secrets in MCP/API errors.
- Serialize autonomous writers targeting the same workspace with a persisted lease.

---

### Task 1: Make AgentService the shared execution boundary

**Files:**
- Modify: `cptr/services/agent_service.py`
- Modify: `cptr/routers/gateway.py`
- Test: `tests/test_agent_service.py`, `tests/test_control_api.py`

- [ ] Add an `AgentService.start_existing_task(...)` adapter that accepts the already-created chat/message, output queue, model target, and workspace, then invokes the existing `chat_task.start_task` without creating a second engine.
- [ ] Replace the gateway’s direct `chat_task.start_task` call with the shared service adapter.
- [ ] Add a regression test asserting the gateway adapter preserves the existing message/chat IDs and output queue contract.
- [ ] Run the focused AgentService and gateway tests.

### Task 2: Add independent verification and failure-signature escalation

**Files:**
- Create: `cptr/services/verification.py`
- Modify: `cptr/services/supervisor.py`
- Modify: `cptr/services/agent_service.py`
- Modify: `cptr/models/control.py`
- Create: `cptr/migrations/versions/0006_control_hardening.py`
- Test: `tests/test_supervisor_core.py`, `tests/test_verification.py`

- [ ] Define an injected `IndependentVerifier` protocol and a default verifier that checks durable worker terminal state, workspace diff evidence, and fixed-argument `git diff --check` results without relying on worker prose.
- [ ] Run the verifier before asking the director to evaluate acceptance, and persist the verifier facts as structured evidence.
- [ ] Add durable per-signature counters to each scope and make normalized signatures drive the retry escalation stages: normal repair, root-cause analysis, alternative strategy, independent-review strategy, then block.
- [ ] Add tests for rejected worker output, repeated cosmetic-equivalent failures, escalation, and successful re-verification.
- [ ] Run the focused supervisor/verifier tests and migration smoke.

### Task 3: Persist evidence, approval requests, and enforcement

**Files:**
- Modify: `cptr/services/supervisor.py`
- Modify: `cptr/services/control_store.py`
- Modify: `cptr/routers/control.py`
- Modify: `cptr/models/control.py`
- Modify: `cptr/migrations/versions/0006_control_hardening.py`
- Test: `tests/test_control_api.py`, `tests/test_supervisor_core.py`

- [ ] Add store methods for append/list evidence and create/get/decide approval records in both in-memory and SQL stores.
- [ ] Persist worker output, verifier facts, director decisions, failures, and final-gate decisions through `AutonomousEvidence` as well as scope summaries.
- [ ] Detect configured external/destructive assignments such as push, deploy, destructive deletion, credential rotation, and costly external actions before delegation.
- [ ] Create a durable `APPROVAL_REQUIRED` record, pause the monitor, enforce approval identity/status, reject stale or duplicate decisions, and resume scheduling only after approval.
- [ ] Expose approval state and evidence from the Control API and test approval pause/resume/deny paths.

### Task 4: Reconcile worker tasks and coordinate workspace recovery

**Files:**
- Modify: `cptr/services/agent_service.py`
- Modify: `cptr/services/control_store.py`
- Modify: `cptr/services/supervisor.py`
- Modify: `cptr/routers/control.py`
- Modify: `cptr/app.py`
- Modify: `cptr/models/control.py`
- Modify: `cptr/migrations/versions/0006_control_hardening.py`
- Test: `tests/test_restart_recovery.py`

- [ ] Add durable reconciliation for control tasks whose in-memory worker disappeared, preserving terminal/error state after restart.
- [ ] Add a persisted workspace lease for autonomous writers and claim it atomically before delegation; release it on terminal/cancel/error paths.
- [ ] On startup reconcile active monitor scopes and existing control tasks before scheduling new work; reuse an eligible existing task instead of creating a duplicate.
- [ ] Add a restart integration test using a temporary SQLite database and a real CPTR app lifecycle that proves a monitor resumes and the worker-task count remains one.
- [ ] Run the restart test and inspect persisted monitor/task/evidence rows directly.

### Task 5: Complete MCP monitor operations and deterministic E2E coverage

**Files:**
- Modify: `server/client/computer-client.ts`
- Modify: `server/mcp.ts`
- Modify: `server/schemas/tools.ts`
- Modify: `server/types.ts`
- Test: `tests/client.test.ts`, `tests/mcp_contract.test.ts`
- Create/Modify: `tests/test_autonomous_e2e.py`

- [ ] Add typed client methods and MCP tools for persistent monitor status, events, evidence, steering, cancellation, and approval while keeping schemas bounded and annotations accurate.
- [ ] Add MCP contract tests for discovery, validation, auth forwarding, error normalization, and no-secret output.
- [ ] Add a deterministic integration test with fake worker/verifier/director components proving reject → diagnose → repair → re-verify → final-gate → `COMPLETE` without ChatGPT polling.
- [ ] Run the plugin test/typecheck/build suite and the Python E2E test.

### Task 6: Security/regression audit, review, and delivery

**Files:**
- Modify: `docs/control-plane.md`
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-08-23-control-plane-production-hardening.md`

- [ ] Audit authentication, workspace ownership, path identity, prompt boundary, secret/log redaction, approval enforcement, retry limits, concurrent monitors, cancellation races, and obsolete hosting material absence.
- [ ] Run existing CPTR checks, focused tests, migration/startup smoke, Control API auth smoke, MCP smoke, plugin checks, and `git diff --check`.
- [ ] Review the complete staged diffs and commit/push only `feat/chatgpt-control-plane` and `feat/initial-mcp-adapter`; do not merge.
- [ ] Report exact repository/branch/base/final/remote SHAs, tests, E2E/restart evidence, limitations, and any remaining external integration action.
