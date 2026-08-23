# ChatGPT Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable authenticated CPTR Control API and a thin MCP adapter that can create and monitor autonomous engineering goals without owning execution outside CPTR.

**Architecture:** Wrap the existing `chat_task` lifecycle in `AgentService`, persist control-plane and supervision state in SQLite/Alembic models, and run one lease-protected supervisor loop per monitor. Keep the plugin as a stateless TypeScript MCP-to-HTTP adapter.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy async, SQLite/Alembic, existing CPTR agent targets and Git helpers, TypeScript, official OpenAI Apps SDK/MCP packages selected from current documentation.

**Spec:** `docs/superpowers/specs/2026-08-23-chatgpt-control-plane-design.md`

## Global Constraints

- Preserve existing `/v1/chat/completions` and UI task behavior.
- Do not create a second worker execution engine or make CPTR call itself over HTTP.
- Treat worker completion as `AGENT_COMPLETE` followed by independent `VERIFYING`.
- Persist original goals and acceptance criteria immutably.
- Use opaque public IDs and enforce workspace ownership plus explicit token scopes.
- Keep external/destructive actions approval-gated.
- Do not expose secrets or chain-of-thought.
- Do not merge feature branches into `main`.

### Task 1: Repository audit and shared service contract

**Files:**
- Create: `docs/superpowers/specs/2026-08-23-chatgpt-control-plane-design.md`
- Create: `docs/superpowers/plans/2026-08-23-chatgpt-control-plane.md`
- Create: `cptr/services/agent_service.py`
- Test: `tests/test_agent_service.py`

**Interfaces:**
- Consumes: `Chat`, `ChatMessage`, `cptr.utils.chat_task.start_task`, `cancel_task`, and existing model-target resolution.
- Produces: `AgentService.start_task`, `get_task`, `get_output`, `send_message`, `cancel_task`, and `get_diff` with stable control-task IDs.

- [ ] Write the failing lifecycle tests for task creation, status, output, steering, cancellation, and terminal persistence.
- [ ] Run the focused tests and confirm they fail because the service and durable control-task record do not exist.
- [ ] Add the minimal models, migration, and service adapter required by those tests.
- [ ] Run the focused service tests and the existing formatter/type checks.
- [ ] Migrate only the new Control API call sites to the service; leave gateway behavior unchanged until compatibility tests pass.

### Task 2: Control-plane persistence and authorization

**Files:**
- Modify: `cptr/models/__init__.py`
- Create: `cptr/models/control.py`
- Create: `cptr/utils/control_auth.py`
- Create: `cptr/routers/control.py`
- Modify: `cptr/app.py`
- Test: `tests/test_control_api.py`

**Interfaces:**
- Consumes: `Workspace`, `AgentService`, existing hashed API-key authentication, and `AuthResult`.
- Produces: versioned workspace/task/git endpoints, scope-aware bearer authentication, stable error payloads, and idempotent task creation.

- [ ] Write failing tests for valid/invalid tokens, missing scopes, workspace ownership, task creation, status/output, messages, cancellation, Git status, Git diff, and repeated idempotency keys.
- [ ] Run the tests to verify the failures are authorization/route/model failures.
- [ ] Implement scoped authentication and workspace ID resolution without exposing raw paths as public identity.
- [ ] Implement the task and Git endpoints through `AgentService` and existing Git helpers.
- [ ] Run the focused API suite and `git diff --check`.

### Task 3: Durable autonomous supervisor

**Files:**
- Create: `cptr/services/supervisor.py`
- Create: `cptr/services/supervisor_director.py`
- Extend: `cptr/models/control.py`
- Create: `cptr/migrations/versions/0005_add_control_plane.py`
- Modify: `cptr/app.py`
- Test: `tests/test_supervisor.py`
- Test: `tests/test_supervisor_recovery.py`

**Interfaces:**
- Consumes: `AgentService`, control persistence, Git evidence helpers, and a provider-neutral `SupervisorDirector`.
- Produces: `create_goal`, `build_scope_ledger`, `start/resume`, `cancel`, `approve`, durable monitor state, evidence, events, and restart reconciliation.

- [ ] Write failing unit tests for scope states, immutable original input, worker success to `VERIFYING`, successful verification, failed verification to `REPAIR_REQUIRED`, automatic repair, multi-scope progression, final-gate repair, and cancellation.
- [ ] Run the tests and confirm they fail before implementation.
- [ ] Implement the persisted monitor state machine with one monitor/workspace lease and idempotent delegation records.
- [ ] Add deterministic fake-director/fake-agent integration coverage for fail-first verification followed by repair and final completion.
- [ ] Add startup recovery that reconciles active monitors and does not duplicate running or terminal worker tasks.
- [ ] Run supervisor and recovery tests, then the full Python test/lint/format checks configured by the repository.

### Task 4: OpenAI-backed director boundary

**Files:**
- Modify: `cptr/services/supervisor_director.py`
- Modify: `cptr/env.py`
- Create: `tests/test_supervisor_director.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: provider-neutral supervisor decision types and environment configuration.
- Produces: structured `evaluate`, `diagnose`, `plan_next_action`, and `final_gate` decisions with persisted response/session identifiers.

- [ ] Write failing mocked tests for valid structured decisions, malformed output, timeout, and provider failure.
- [ ] Verify the tests fail before adding provider code.
- [ ] Consult current official OpenAI documentation and implement the isolated Responses API client with configurable model and timeouts.
- [ ] Ensure persisted data contains structured decisions only and never chain-of-thought.
- [ ] Run mocked director tests and configured type/lint checks.

### Task 5: MCP adapter repository

**Files:**
- Create: `server/index.ts`
- Create: `server/mcp.ts`
- Create: `server/client/computer-client.ts`
- Create: `server/tools/*.ts`
- Create: `server/schemas/*.ts`
- Create: `server/auth/*.ts`
- Create: `tests/*.test.ts`
- Create: `.env.example`
- Create: `package.json`
- Create: `tsconfig.json`
- Create: `README.md`

**Interfaces:**
- Consumes: `/api/control/v1` stable JSON responses and environment configuration.
- Produces: nine annotated MCP tools with explicit schemas, bounded validation, normalized errors, and no long-lived polling loop.

- [ ] Inspect current official Apps SDK/MCP documentation and the closest official example; record package choices in the README.
- [ ] Write failing contract tests for server initialization, tool discovery, annotations, auth forwarding, timeouts, and each tool.
- [ ] Run the plugin tests to verify the expected failures.
- [ ] Implement the typed HTTP client, schemas, tools, and MCP endpoint.
- [ ] Run plugin tests, typecheck, lint, build, and an MCP enumeration smoke test.

### Task 6: Integration, security, and handoff

**Files:**
- Modify: `computer/README.md`
- Modify: `chatgpt-computer-plugin/README.md`
- Create: `docs/integration-evidence.md`
- Create: `docs/security-audit.md`

**Interfaces:**
- Consumes: completed Control API, supervisor, director, and plugin.
- Produces: documented setup, state machine, security model, approval boundary, recovery behavior, ChatGPT Developer Mode connection steps, known limitations, and evidence.

- [ ] Start CPTR locally with a test data directory and verify existing health/UI/gateway behavior.
- [ ] Start the plugin and verify MCP initialization plus tool enumeration.
- [ ] Run a deterministic end-to-end task through MCP, including fail-first verification and automatic repair.
- [ ] Restart CPTR during an active monitor and verify recovery without duplicate worker tasks.
- [ ] Audit authorization, path handling, idempotency, cancellation, approval, retry, secret redaction, and concurrent workspace monitors.
- [ ] Run final `git status --short`, `git diff --check`, focused suites, configured lint/typecheck/build commands, and record actual pass/fail results.
