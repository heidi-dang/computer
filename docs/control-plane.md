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
```

When both director settings are present, CPTR uses the provider-neutral `SupervisorDirector` interface with the OpenAI Responses implementation and structured JSON-schema decisions. Response IDs are persisted for continuation. Without those settings, the local conservative director is used for local development; production deployments should configure the director and independently verify the resulting evidence.

### Performance and durability tuning

The backend ships with conservative defaults suitable for a local/single-host CPTR instance. Every value below is optional and should be changed from measurement rather than by blindly increasing concurrency:

```text
CPTR_DB_BUSY_TIMEOUT_MS=5000
CPTR_DB_CACHE_SIZE_KIB=32768
CPTR_DB_MMAP_SIZE_BYTES=134217728
CPTR_DB_WAL_AUTOCHECKPOINT_PAGES=1000
CPTR_DB_SYNCHRONOUS=NORMAL

CPTR_COMMAND_OUTPUT_BUFFER_BYTES=262144
CPTR_COMMAND_READ_CHUNK_BYTES=16384
CPTR_COMMAND_SESSION_TTL_SECONDS=900
CPTR_COMMAND_SESSION_MAX_RETAINED=128
CPTR_COMMAND_SESSION_REAPER_INTERVAL_SECONDS=30
CPTR_COMMAND_LOG_MAX_BYTES=52428800
CPTR_COMMAND_LOG_BATCH_BYTES=131072
CPTR_COMMAND_LOG_FLUSH_INTERVAL_MS=200
CPTR_COMMAND_LOG_QUEUE_SIZE=256
CPTR_COMMAND_EVENT_QUEUE_SIZE=64
CPTR_TERMINAL_EVENT_COALESCE_BYTES=8192
CPTR_TERMINAL_EVENT_FLUSH_INTERVAL_MS=100

CPTR_LIVE_EVENT_WRITE_BATCH_SIZE=64
CPTR_LIVE_EVENT_QUEUE_SIZE=2048
CPTR_LIVE_EVENT_RETENTION_CLEANUP_INTERVAL=100
CPTR_CONTROL_AUTH_CACHE_TTL_SECONDS=30
CPTR_CONTROL_AUTH_CACHE_MAX_ENTRIES=512
CPTR_DIRECT_CODING_IO_CONCURRENCY=4

CPTR_FDX_ENABLED=true
CPTR_FDX_BINARY=
CPTR_FDX_REQUEST_TIMEOUT_SECONDS=20
CPTR_FDX_DAEMON_IDLE_TTL_SECONDS=600
CPTR_FDX_MAX_DAEMONS=8
CPTR_FDX_MAX_RESPONSE_BYTES=262144
```

SQLite uses WAL mode, enforces foreign keys, applies a bounded busy timeout, and configures cache/checkpoint settings on every connection. `CPTR_DB_SYNCHRONOUS=NORMAL` is the default performance/durability balance; operators requiring stronger fsync semantics can choose `FULL` or `EXTRA` after measuring the impact.

Command PTY capture is intentionally decoupled from log and live-event durability. The in-memory output ring and durable JSONL command log remain authoritative even if a slow live subscriber falls behind. Live terminal queues are bounded; when a live terminal cannot keep up, dropped live bytes are counted and the terminal can recover from bounded replay/status surfaces without allowing unbounded backend memory growth.

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
POST /workspaces/{workspace_id}/coding/fdx
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

API-key metadata is stored in an indexed `control_api_keys` table for hot-path authentication and cached for a short bounded TTL. Existing installations that stored `api_keys` inside the config JSON are migrated automatically on startup and the compatibility mirror is retained when keys are changed.

## Direct-coding safety boundary

Direct-coding requests are bound to an owned workspace ID and accept only paths relative to that workspace. CPTR rejects absolute paths, traversal attempts, and environment-file paths. Reads reject binary files and files over 500 KB; writes and edits are capped at 1 MB. Exact edits require one unambiguous matching target. Command sessions are bounded, owned by the authenticated user, and support status, incremental output, and cancellation. CPTR rejects destructive command patterns and requires both an explicit `allow_network` flag and the separate `command:external` scope for commands that may contact external services.

The direct-coding tools are deliberately distinct from the broader internal CPTR agent-tool registry. ChatGPT can autonomously chain the exposed coding primitives but is not given direct access to credentials, arbitrary host paths, CPTR browser sessions, deployment controls, or unconstrained internal tools.

Directory listing is structured and bounded at traversal time; a shallow list never recursively counts every child file. Batch reads use bounded I/O concurrency, and search context groups matches by source file so one file is not reread for every match.

### FDX-first repository intelligence

`POST /workspaces/{workspace_id}/coding/fdx` is the backend for the single ChatGPT-facing `cptr_fdx_intelligence` action. It requires only `coding:read` and is deliberately structured rather than accepting arbitrary FDX command strings. ChatGPT chooses one bounded intelligence action such as `read`, `search`, `grep`, `batch`, `outline`, `tree`, `impact_v2`, `why`, semantic/build diagnostics, `diff`, `index_status`, or `plan`. FDX never performs agent delegation through this route, and exact CPTR file reads plus SHA-256 preconditions remain authoritative before mutation.

The selected `repo_path` is relative to the authorized workspace or, when `worker_id` is supplied, to that Direct Coding Worker's isolated Git worktree. Repository-bound FDX operations refuse to walk above that root: if the selected directory is not itself a Git repository root, CPTR returns a typed degraded result and recommends normal Direct Coding tools instead. This protects nested-workspace deployments from FDX repository discovery crossing the CPTR authorization boundary.

CPTR prefers a persistent `fdx serve --root <authorized-repository>` process for daemon-capable intelligence and negotiates protocol/capabilities before use. Daemons are keyed by user, workspace, and resolved repository root, bounded by `CPTR_FDX_MAX_DAEMONS`, reaped after the configured idle TTL, and terminated during CPTR shutdown. Other read-only FDX intelligence actions run as argv-based subprocesses without a shell. Returned paths are converted to repository-relative values where possible; host paths and oversized output are redacted or bounded before leaving CPTR.

FDX binary discovery is explicit and local-only: `CPTR_FDX_BINARY`, then the execution identity's `~/.cptr/bin/fdx`, then the standard Cargo install location `~/.cargo/bin/fdx`, then `CPTR_DATA_DIR/bin/fdx`, then `fdx` on that identity's `PATH`. If FDX is disabled, unavailable, incompatible, times out, or returns degraded evidence, the response sets `fallback_recommended=true` and ChatGPT continues with the ordinary CPTR tree/search/read/Git primitives rather than failing the Direct Coding task.

## Health and metrics

Service supervision can use:

```text
GET /api/health       # compatibility liveness surface
GET /api/health/live  # process liveness
GET /api/health/ready # SQLite readiness; 503 when unavailable
GET /api/metrics      # authenticated administrator only
```

The metrics snapshot is bounded and contains no prompt, command output, credentials, or file content. It reports request and database latency percentiles, database errors/busy events, SQLite database/WAL/SHM sizes, event-loop lag, command-session retention/output bytes, live-event queue/subscriber state, API-key cache size, RSS memory, and open file descriptors where the platform exposes them.

## Execution-plane scaling boundary

CPTR still runs one API/execution process by default. Command process handles, browser ownership, live subscribers, and several coordination caches are intentionally process-local. Do **not** increase Uvicorn worker count as a performance workaround: another worker cannot safely inherit ownership of a command or browser session started elsewhere.

The command-session registry is now isolated behind an execution-manager service boundary so it can later move behind durable IPC without changing the direct-coding API. Multi-worker serving becomes safe only after command/browser ownership, live subscription fan-out, and relevant idempotency state are externalized to that execution plane. See `docs/architecture/execution-plane.md`.

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

- CPTR remains a single-process execution owner; multiple Uvicorn workers are intentionally unsupported until the execution plane is externalized behind durable IPC.
- The first pass has no widget.
- The local director is a deterministic development fallback; production autonomous verification should use the configured director and real evidence.
- CPTR inherits its host-level single-user filesystem/shell security model. It should not be exposed to untrusted users without an appropriate authentication and network boundary.
- The existing CPTR repository has pre-existing full-tree lint findings; new control-plane files are checked separately and cleanly.
