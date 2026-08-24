# CodeAct read-only foundation review

Status: implemented, disabled by default, and not a production feature
enablement.

## Architectural boundary

CPTR remains the only model owner and FlowDeck remains the only authority,
lease, fencing, cancellation, evidence, and transcript owner:

```text
authenticated CPTR request
  -> FlowDeck gateway and durable attempt
  -> server-selected execution mode
     -> native CPTR tool loop
     -> read-only CodeAct adapter
        -> attempt-scoped REPL host
        -> short-lived restricted worker
        -> host-owned capability SDK
        -> existing CPTR execute_tool policy
```

The CodeAct package is a composition layer below that boundary. It cannot
create a run, select a model, issue a lease, complete an attempt, write a
transcript, or delegate. The normal client contract cannot select CodeAct;
the internal FlowDeck dispatch must supply a server-generated program.

Execution modes are an explicit closed set. Native tool calling is the
default, read-only CodeAct is the only non-native mode represented, and an
unknown mode fails closed rather than silently falling back.

## Session lifecycle

One physical FlowDeck attempt owns one REPL session identity. The host starts
the worker lazily on the first enabled execution, serializes blocks with an
execution lock, preserves namespace state within that attempt, and never
shares state between attempts. Normal completion, cancellation, timeout,
worker failure, parent failure, and context exit close the process and pipes.
A disabled context fails before spawning a child.

The worker protocol is JSON-lines over private stdin/stdout. Protocol messages
and captured program output are separate. Capability implementations remain in
the host process; the worker can only request names explicitly present in the
host-created SDK.

## Capability and import policy

The initial SDK is read-only and maps only to existing CPTR file policy:

- `cptr.files.read`
- `cptr.files.list`
- `cptr.files.search`

No shell, terminal, raw filesystem, Git mutation, browser mutation, network,
environment, package installation, secret, MCP, FDX, deployment, or
delegation capability is exposed. The adapter reuses CPTR's authenticated
workspace and tool guard instead of implementing a parallel policy.

The worker allows only a small audited standard-library import set:
`collections`, `datetime`, `functools`, `itertools`, `json`, `math`, `re`, and
`statistics`. AST validation blocks dynamic imports, introspection, code
generation, filesystem access, environment access, subprocess/network access,
serialization escape primitives, and unsafe object attributes. Safe builtins
are explicitly enumerated.

## Resource and threat model

The host enforces code size, output size, capability-call, wall-clock, CPU,
and bounded address-space limits. The worker has no credentials, request
object, workspace path authority, service client, or arbitrary callable.
Timeout and cancellation terminate the process group; late worker events are
rejected by the durable attempt identity before native transcript emission.

The principal threats are prompt-generated escape code, capability argument
abuse, stale completion after cancellation/recovery, cross-attempt state
leakage, output/protocol confusion, resource exhaustion, and accidental
authority expansion. The controls above address each at the current
read-only boundary. A passing provider qualification report is required before
any role/model could be admitted, and the existing report currently keeps the
feature disabled. Mutation CodeAct and broader sandbox claims are explicitly
out of scope.

## Qualification and disabled-mode invariant

Qualification compares the existing CPTR provider through native and CodeAct
arms on the same fixed read-only corpus and records correctness, usage,
latency, capability, and adversarial escape evidence. Partial, duplicate,
fixture-only, stale-model, or failed-provider evidence cannot enable the
feature.

The default configuration remains `CPTR_CODEACT_MODE=disabled`. Tests prove
that disabled CodeAct rejects execution without spawning a worker, while
FlowDeck's default `tool_calling` path retains the native CPTR loop and
durable lifecycle. No API process uses unrestricted `exec`; generated code is
handled only by the separately launched restricted worker after validation.