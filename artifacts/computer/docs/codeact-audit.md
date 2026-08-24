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
harness. `cptr.codeact.benchmark.run_provider_benchmark` is the closure entry
point: the caller supplies the existing CPTR provider runner and one model id,
and both arms receive the same task corpus and model. Each observation records
input/output/total tokens, cycles, capability calls, context bytes, latency, and
correctness. The report also includes import, introspection, filesystem,
environment, socket, subprocess, and serialization escape results.

The score is `60% correctness + 40% blocked escape cases`. The report can only
recommend `enable-read-only` when every supplied task is correct and every
adversarial case is blocked; otherwise the explicit decision is
`keep-disabled`. A provider-backed report requires a non-empty corpus and must
be run with the configured CPTR connection, not fixture callbacks. Mutation
CodeAct remains out of scope.

## Live qualification procedure

`python -m cptr.codeact.qualification --model <configured-model-id>` resolves
one existing CPTR API model and runs it through both arms. The runner uses the
same fixed three-case corpus in both modes: extracting a release label, adding
two inventory values, and identifying a ready record's owner. Its fixtures are
in-memory and expose only `read_file`/`list_directory` in the native arm and
the matching `cptr.files.read`/`cptr.files.list` SDK in CodeAct. This keeps the
qualification reproducible and proves model protocol behavior without granting
the corpus any real workspace authority.

The command writes `docs/codeact-qualification-report.json`. Each observation
contains the resolved model id, input/output/total tokens, model cycles,
capability calls, context bytes, wall latency, and correctness. The same report
includes all seven sandbox escape categories, a weighted score, and the
explicit enable/keep-disabled decision. The runner decrypts an already-stored
CPTR connection only in process; it neither accepts nor writes credentials.

## Latest live result

The provider-backed run against `minimax-m3` completed with all three native
tool-call cases correct, one of three CodeAct cases correct, and all seven
escape categories blocked. Its weighted score is `80.0`, and the report's
explicit decision is `keep-disabled`. CodeAct must remain disabled for this
model until a fresh live qualification reaches complete correctness.

Production CodeAct also fails closed on the report: in addition to the existing
mode, role, and kill-switch controls, `CPTR_CODEACT_QUALIFICATION_REPORT` must
name a readable report with a model-matching `enable-read-only` decision,
`100.0` score, fully correct observations, and all seven escapes blocked. The
report must contain the exact three deterministic cases in both native and
CodeAct modes, plus each named escape category; partial or duplicate results
cannot unlock CodeAct.