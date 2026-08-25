# CPTR Control Plane

The CPTR control plane is a versioned API for external clients such as the companion ChatGPT MCP adapter. It is deliberately separate from the OpenAI-compatible `/v1/chat/completions` gateway.

## Ownership

`computer` owns workspace authorization, worker task execution, durable task projections, autonomous goals and scope ledgers, evidence, verification, retry escalation, approvals, and restart recovery. The companion `chatgpt-computer-plugin` owns only MCP transport, schemas, annotations, and HTTP forwarding.

The MCP connection does not own the monitor loop. ChatGPT can disconnect after `cptr_monitor_autonomous` returns while CPTR continues supervising in the background.

## Configuration

The CPTR process reads these optional settings from the environment:

```text
CPTR_SUPERVISOR_POLL_INTERVAL=2
CPTR_SUPERVISOR_MAX_ATTEMPTS=5
CPTR_SUPERVISOR_OPENAI_API_KEY=<secret>
CPTR_SUPERVISOR_OPENAI_MODEL=<configured-model-id>
CPTR_OPENAI_BASE_URL=https://api.openai.com/v1
CPTR_TASK_ROOT=~/.cptr/runs
```

When both director settings are present, CPTR uses the provider-neutral `SupervisorDirector` interface with the OpenAI Responses implementation and structured JSON-schema decisions. Response IDs are persisted for continuation. Without those settings, the local conservative director is used for local development; production deployments should configure the director and independently verify the resulting evidence.

`CPTR_TASK_ROOT` is the central location for task-owned transient runtime state. CPTR places provider caches/configuration, staged attachments, command output logs, and browser runtime state below one directory per task. The default is `$CPTR_DATA_DIR/runs`. The selected project workspace remains the agent's working directory, and the existing workspace-local `.cptr/` metadata layout is preserved for compatibility.

## Control API

The authenticated API is rooted at `/api/control/v1`:

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
POST /workspaces/{workspace_id}/coding/list
POST /workspaces/{workspace_id}/coding/read
POST /workspaces/{workspace_id}/coding/search
POST /workspaces/{workspace_id}/coding/write
POST /workspaces/{workspace_id}/coding/edit
POST /workspaces/{workspace_id}/coding/commands
GET  /workspaces/{workspace_id}/coding/commands/{command_id}
POST /workspaces/{workspace_id}/coding/commands/{command_id}/cancel
POST /autonomous
GET  /autonomous/{monitor_id}
GET  /autonomous/{monitor_id}/events
GET  /autonomous/{monitor_id}/evidence
POST /autonomous/{monitor_id}/messages
POST /autonomous/{monitor_id}/cancel
POST /autonomous/{monitor_id}/approve
```

Public identities are opaque workspace, task, goal, monitor, and scope IDs. Workspace paths are metadata, not identity keys. All resources are checked against the authenticated owner.

## Scopes and credentials

Control-plane bearer tokens are validated by CPTR. The initial key scopes are:

```text
workspace:read
task:read
task:write
autonomous:run
git:read
coding:read
coding:write
command:execute
command:external (optional; not issued by default)
```

The direct-coding API is designed for an official ChatGPT MCP connector. It performs no CPTR model selection and does not invoke the CPTR agent loop: ChatGPT itself chooses and sequences scoped file and command tools. `coding:read` is required for list/read/search; `coding:write` is required for file writes and exact edits; `command:execute` is required for managed workspace commands; `command:external` is additionally required for explicitly approved commands that may contact external services. `git:write` and `deploy:write` remain reserved. The MCP adapter is not trusted merely because a request originated in ChatGPT. CPTR checks the token, required scope, user ownership, and resource identity.

New keys issued through `POST /v1/keys` receive the default direct-coding scopes. An authenticated administrator may send an explicit `scopes` array to issue a least-privilege custom key; CPTR accepts only the documented scopes and rejects unknown values. `command:external` is optional and must be explicitly included when an operator intends to permit approved external commands.

## Direct-coding safety boundary

Direct-coding requests are bound to an owned workspace ID and accept only paths relative to that workspace. CPTR rejects absolute paths, traversal attempts, and environment-file paths. Reads reject binary files and files over 500 KB; writes and edits are capped at 1 MB. Exact edits require one unambiguous matching target. Command sessions are bounded, owned by the authenticated user, and support status, incremental output, and cancellation. CPTR rejects destructive command patterns and requires both an explicit `allow_network` flag and the separate `command:external` scope for commands that may contact external services.

The direct-coding tools are deliberately distinct from the broader internal CPTR agent-tool registry. ChatGPT can autonomously chain the exposed coding primitives but is not given direct access to credentials, arbitrary host paths, CPTR browser sessions, deployment controls, or unconstrained internal tools.

## Autonomous state machine

The supervisor persists the original goal and acceptance criteria as immutable inputs. Each acceptance criterion becomes an explicit scope ledger entry. A worker reporting success follows this path:

```text
PENDING → WORKING → AGENT_COMPLETE → VERIFYING → VERIFIED
                              ↘ REPAIR_REQUIRED → WORKING
```

The monitor reaches `COMPLETE` only when every required scope is `VERIFIED` and the final gate passes. A failed worker, failed verification, or failed final gate creates repair evidence and an explicit next action. Repeated normalized failures escalate through the configured attempt limit and then become `BLOCKED`.

Independent verification records durable worker terminal state, repository status, and a fixed-argument `git diff --check` result. Worker prose is evidence presented to the director, not proof of completion. Verifier facts, worker output, director decisions, failures, approval requests, and final-gate decisions are appended to `autonomous_evidence` and exposed by the evidence endpoint.

External or destructive actions pause in `APPROVAL_REQUIRED` with a persisted approval ID, operation, reason, timestamp, and status. Approval is accepted only for the currently pending approval record.

Assignments containing push, deployment/release, destructive storage/database deletion, credential rotation, or costly external operations create a durable approval request and do not delegate until the matching pending approval is approved. Duplicate, stale, cross-monitor, and already-decided approvals are rejected. Approved operation prefixes are persisted so restart does not re-prompt or bypass the approval boundary.

## Restart recovery

Monitor state, scope state, attempts, evidence, approvals, and worker task IDs are stored in SQLite. CPTR startup finds active monitors, claims a lease, reconciles worker task state from durable messages, and resumes eligible monitors. The lease and task idempotency key prevent duplicate worker delegation after concurrent resume or process restart.

If CPTR restarts after creating a worker task but before saving the scope transition, recovery retries the same deterministic monitor/scope/attempt idempotency key and reuses the existing durable `ControlTask`. If the in-memory worker disappeared, `AgentService` reconciles its durable chat message as an interrupted failure and the supervisor diagnoses or retries it. Autonomous writer monitors also claim a persisted workspace lease so concurrent monitors targeting the same workspace wait rather than editing concurrently.

## Local verification

The focused Python suite is run with:

```bash
python -m unittest discover -s tests -v
```

The repository's frontend checks remain unchanged. The control-plane migration is applied by the existing Alembic startup path.

Independent workspace validation can be configured with `CPTR_VERIFICATION_COMMANDS_JSON`, a JSON
array of argv-based commands. Each item has a `name`, a `category`, an `argv` array, and an optional
`timeout_seconds` value (capped at ten minutes). Categories are `focused_tests`, `broader_tests`,
`lint`, `typecheck`, `build`, and `runtime_smoke`. CPTR executes these commands in the owned
workspace without a shell and persists the category, bounded stdout/stderr, timestamps, duration,
exit code, timeout state, and pass/fail evidence. Worker output is never treated as validation
evidence. Existing command entries without a category remain compatible and are recorded as
`runtime_smoke`.

For example:

```json
[
  {"name":"focused-tests","category":"focused_tests","argv":["python","-m","unittest","discover","-s","tests"]},
  {"name":"lint","category":"lint","argv":["ruff","check","."]},
  {"name":"runtime-smoke","category":"runtime_smoke","argv":["python","scripts/smoke.py"]}
]
```

## ChatGPT Developer Mode

Start CPTR and the companion MCP adapter, expose the adapter through an HTTPS tunnel or deployment, and add the adapter's `/mcp` URL in ChatGPT Developer Mode under Settings → Connectors. Use the plugin README for adapter-specific commands. Refresh the connector after changing tool schemas or annotations.

## Known limitations

- The first pass has no widget.
- The local director is a deterministic development fallback; production autonomous verification should use the configured director and real evidence.
- CPTR inherits its host-level single-user filesystem/shell security model. It should not be exposed to untrusted users without an appropriate authentication and network boundary.
- The existing CPTR repository has pre-existing full-tree lint findings; new control-plane files are checked separately and cleanly.
