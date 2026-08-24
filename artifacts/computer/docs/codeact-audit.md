# CPTR CodeAct architecture and qualification boundary

## Native call graph

The default path remains:

`HTTP chat/FlowDeck request` → authenticated gateway → durable run/step/operation/attempt
→ `start_task`/native subagent registry → `run_chat_task` → CPTR model target
→ `execute_tool` → existing filesystem/Git/browser/tool boundaries → native CPTR events
→ persisted `ChatMessage` and Socket.IO transcript.

CodeAct is an execution strategy below the authenticated FlowDeck boundary:

`authenticated gateway` → read-only specialist durable attempt → CPTR-generated program
→ `run_read_only_attempt` → `CodeActRepl` → isolated worker JSON-RPC
→ server-owned capability adapter → existing `execute_tool` policy → native capability
implementation → identity-bound lifecycle/output events.

The worker is not a model, transport, lease authority, or transcript renderer.
It receives no workspace path authority, request object, credentials, service client,
or arbitrary callable.

## Controls

- `CPTR_CODEACT_MODE` defaults to `disabled`; only `read_only` is implemented.
- `CPTR_CODEACT_READ_ONLY_ROLES` is an explicit allowlist.
- `CPTR_CODEACT_KILL_SWITCH` disables all CodeAct execution.
- CodeAct cannot be selected by the normal client payload; the internal FlowDeck
  request must provide a server-generated program and still pass role policy.
- The initial SDK maps only read-only file operations. No writes, terminal,
  deployment, package installation, secrets, MCP, browser mutation, or delegation
  capability is exposed.

## Isolation and recovery

Each physical attempt gets a fresh persistent worker process. Code blocks share
that attempt's namespace, but separate attempts do not. The parent owns the RPC
loop, capability authorization, wall timeout, cancellation, and teardown. The
worker uses a restricted builtin table, import allowlist, AST validation, CPU and
address-space ceilings, and no host filesystem/environment API. Child exit,
timeout, cancellation, and parent failure close the process and its pipes.

The configured address-space ceiling has a 1 GiB minimum floor because the
container's Python launcher reserves a larger virtual arena before worker
startup; generated code remains bounded by CPU, output, code, calls, and wall
limits.

## Qualification status

The read-only engine has focused adversarial/lifecycle tests and an A/B telemetry
harness. A production score is intentionally not claimed until restart,
concurrency, full FlowDeck cancellation, and same-model benchmark runs complete
against the live CPTR provider. CodeAct remains disabled by default and mutation
CodeAct is out of scope.