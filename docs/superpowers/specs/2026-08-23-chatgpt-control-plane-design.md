# ChatGPT Control Plane and Autonomous Supervision Design

## Goal

Expose CPTR's existing agent execution engine through a durable, authenticated `/api/control/v1` boundary and let a server-side autonomous supervisor continue work, verification, repair, and final acceptance after the ChatGPT/MCP connection ends.

## Repository boundaries

The `computer` repository remains the execution engine. It owns workspace authorization, task creation and lifecycle, persistent supervisor state, worker reconciliation, evidence, approvals, retry policy, and the OpenAI-backed director implementation.

The `chatgpt-computer-plugin` repository is a thin MCP adapter. It owns `/mcp`, tool schemas and annotations, environment-based configuration, scoped credential forwarding, and translation between MCP requests and the CPTR Control API. It does not run workers, persist goals, or poll forever on behalf of a connected client.

The repositories are independent Git roots. The implementation branches are `feat/chatgpt-control-plane` and `feat/initial-mcp-adapter`; neither feature branch is merged into `main` as part of this work.

## Existing CPTR integration points

The current gateway is registered from `cptr/app.py` and exposes `/v1/models` and `/v1/chat/completions`. It resolves a workspace, creates or reuses a chat, creates an assistant message, starts `cptr.utils.chat_task.start_task`, and consumes an in-memory `asyncio.Queue` for streaming.

`cptr/utils/chat_task.py` owns the running asyncio task registry, live output state, cancellation, persisted `ChatMessage` updates, event publication, and startup reconciliation for interrupted chat messages. The control plane will call an internal service boundary around this lifecycle instead of making HTTP calls back into CPTR or creating a second worker engine.

Workspace ownership is represented by `Workspace.user_id` plus its persisted path. Existing cookie/session and gateway API-key authentication remain compatible. Control-plane bearer tokens are validated by CPTR and resolved to a user plus explicit scopes; a token is not trusted solely because the request came from the MCP adapter.

## Durable domain model

The first migration adds dedicated tables for control tasks, autonomous monitors, scope ledger entries, evidence, approvals, and idempotency records. Every row has a stable opaque identifier and timestamps. Public responses expose workspace IDs and task/monitor IDs, never raw filesystem paths as identity contracts.

The monitor stores the original goal and acceptance criteria as immutable JSON/text inputs. Scope rows store the derived title, description, criteria, current status, attempt count, worker task IDs, verification and failure evidence, last decision, next action, and timestamps. Subsequent worker or director summaries are appended as evidence and decisions; they never overwrite the original goal.

The minimum scope states are `PENDING`, `ASSIGNED`, `WORKING`, `AGENT_COMPLETE`, `VERIFYING`, `REPAIR_REQUIRED`, `VERIFIED`, `BLOCKED`, and `CANCELLED`. Monitor states are `RUNNING`, `APPROVAL_REQUIRED`, `BLOCKED`, `FAILED`, `CANCELLED`, and `COMPLETE`.

Control tasks reference the underlying CPTR chat/message IDs and persist status, output snapshots, workspace ownership, cancellation state, and idempotency keys. Live queues remain an optimization for connected clients; task status and output are available from durable `ChatMessage` state after restart.

## AgentService boundary

`AgentService` is the only service used by the Control API and supervisor to start or steer worker tasks. Its async interface is:

```python
class AgentService:
    async def start_task(...): ...
    async def get_task(...): ...
    async def get_output(...): ...
    async def send_message(...): ...
    async def cancel_task(...): ...
    async def get_diff(...): ...
```

The initial implementation adapts existing chat/message creation and `chat_task.start_task` calls. Existing gateway and UI routes continue to use their current behavior; shared helpers are extracted only where the same persisted lifecycle can be safely reused. The service returns stable control-task records and delegates execution to the existing agent stack.

## Supervisor cycle

Creating a monitor persists the goal, builds a scope ledger, and schedules one resumable worker loop. The loop observes worker state, collects output and Git evidence, evaluates the current scope, diagnoses failures, delegates follow-up work when required, and waits for a terminal worker state before repeating.

The invariant is `WORKER COMPLETE != GOAL COMPLETE`. A successful worker transitions its scope to `VERIFYING`. A scope becomes `VERIFIED` only when structured independent verification evidence satisfies its immutable acceptance criteria. The final gate evaluates every original scope and the original goal; it may create repair work rather than accepting only the latest task's output.

Monitor creation and delegation are idempotent. A persisted lease prevents two process tasks from advancing the same monitor simultaneously. On startup, CPTR finds active monitors, reconciles referenced worker tasks, preserves completed evidence and counters, and resumes only eligible monitors. It does not create a duplicate worker for a task already in a terminal or running state.

## Director boundary

The supervisor depends on a provider-neutral `SupervisorDirector` with `evaluate`, `diagnose`, `plan_next_action`, and `final_gate` methods. The OpenAI Responses API implementation is isolated behind that interface. Model and timeout configuration come from environment/configuration, never hard-coded model IDs. Persisted director state contains provider response/session identifiers and structured decisions/evidence only; it never stores or exposes chain-of-thought.

Malformed or unavailable director responses become structured `FAILED`/`BLOCKED` decisions according to policy. The decision schema includes `scope_satisfied`, `goal_satisfied`, `defects`, `regressions`, `next_action_required`, `next_assignment`, and `blocking_reason`.

## Retry and approval policy

Failures are normalized into signatures built from stable error categories, affected scope, and relevant verification facts. Cosmetic log differences do not reset escalation. Configurable escalation defaults to five attempts: normal repair, root-cause re-analysis, alternative strategy, independent reviewer strategy, then blocking/escalation.

Operations with external or destructive side effects are represented as persisted approval requests containing `approval_id`, operation, reason, requested time, and status. The monitor enters `APPROVAL_REQUIRED` and resumes only after an authorized approval for the same action is recorded. Local reads, edits, shell commands, tests, lint, type checks, builds, Git inspection, and worker execution remain policy-controlled local operations.

## Control API

The new router is versioned under `/api/control/v1` and exposes:

```text
GET  /workspaces
GET  /workspaces/{workspace_id}
POST /tasks
GET  /tasks/{task_id}
GET  /tasks/{task_id}/output
POST /tasks/{task_id}/messages
POST /tasks/{task_id}/cancel
GET  /workspaces/{workspace_id}/git/status
GET  /workspaces/{workspace_id}/git/diff
POST /autonomous
GET  /autonomous/{monitor_id}
GET  /autonomous/{monitor_id}/events
GET  /autonomous/{monitor_id}/evidence
POST /autonomous/{monitor_id}/messages
POST /autonomous/{monitor_id}/cancel
POST /autonomous/{monitor_id}/approve
```

All endpoints authenticate through CPTR and authorize the resolved user against the workspace/task/monitor owner. Control tokens support `workspace:read`, `task:read`, `task:write`, `autonomous:run`, and `git:read`. `git:write` and `deploy:write` remain reserved and are not granted to the initial plugin token.

## MCP adapter

The plugin implements nine tools: `cptr_list_workspaces`, `cptr_get_workspace`, `cptr_start_task`, `cptr_monitor_autonomous`, `cptr_get_task`, `cptr_get_task_output`, `cptr_send_message`, `cptr_cancel_task`, and `cptr_get_diff`. Each tool has explicit bounded input validation, output typing, usage-oriented descriptions, correct read/write/destructive annotations, and normalized non-secret errors.

Configuration is supplied through `CPTR_BASE_URL`, `CPTR_API_TOKEN`, `HOST`, and `PORT`. No hostname, port, token, username, model, or filesystem path is hard-coded. The MCP server forwards the configured token as a bearer credential and does not expose it in tool results or error messages.

## Testing and acceptance

New Python tests cover service lifecycle, authorization, idempotency, scope transitions, immutable goals, verification failures and repairs, retry escalation, approval pauses, cancellation, restart recovery, and concurrent resume protection. A deterministic integration test forces the first verification to fail, observes automatic repair delegation, then verifies completion and the final gate.

Plugin tests cover MCP initialization, tool schemas/annotations, client authentication forwarding, timeout/error normalization, and every V1 tool using a mocked Control API. Typecheck, lint, and build checks run using the repository's selected Node tooling.

The final runtime path must demonstrate normal task execution, authenticated control-plane access, MCP tool enumeration, autonomous repair, and restart recovery without duplicate worker tasks. Known limitations are documented in both repositories, including the single-user host security model inherited from CPTR and the absence of a widget in this first pass.
