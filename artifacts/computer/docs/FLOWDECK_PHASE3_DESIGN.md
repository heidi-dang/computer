# FlowDeck-C-PTR Phase 3 design freeze

This document specifies the durable foundation plus future contracts. The
foundation is a state-and-recovery layer, not an execution runtime; future
adapter and specialist behavior described below remains unimplemented.

## Durable foundation delivered by Task #2

The first durable foundation is now implemented as a state-and-recovery layer,
not an execution engine. SQLite persists `runs`, `steps`, logical operation
intents, physical attempts, versioned events, workspace mutation leases, and
exclusive recovery leases. The layer never invokes a provider, tool, command,
agent, adapter, or mutation; CPTR remains the execution owner.

The durable lifecycle is:

```text
PENDING → RUNNING → SUCCEEDED | FAILED
                    └→ ORPHANED → RECOVERING → terminal | MANUAL_REVIEW_REQUIRED
```

An operation is `INTENT_RECORDED` before an attempt can be prepared. Each retry
keeps the logical operation ID and receives a new physical attempt ID. An
interrupted attempt becomes `OUTCOME_UNKNOWN`; only verifier/runtime evidence
with the operation-specific reconciliation contract can produce a terminal
outcome. Otherwise it remains or becomes `MANUAL_REVIEW_REQUIRED`.

The durable state is authoritative. CPTR's in-memory live task map may be used
only as a liveness hint and cannot establish completion, retry safety, or
reconciliation.

## Execution boundary

The existing CPTR primitive to wrap is
`cptr.utils.chat_task.run_chat_task`. CPTR's `start_task` creates the native
asyncio task, and existing async subagents re-enter the same primitive through
`cptr.utils.tools._run_existing_subagent_chat`. The future boundary is:

```text
FlowDeck scheduler → SubagentExecutionPort → CPTR run_chat_task
```

FlowDeck must not introduce a second model/tool loop, duplicate compaction,
duplicate approvals, or a competing stream transport.

## Durable ordering and identity

1. Create a logical operation intent before any workspace or external mutation.
2. Assign a stable logical operation ID to the requested operation.
3. Assign a separate physical attempt ID to each attempt.
4. Record the intended capability, target, workspace ownership, and expected
   verifier before invoking a mutation adapter.
5. Record evidence and verification after the adapter returns.

An interrupted adapter call has `OUTCOME_UNKNOWN` until positively reconciled.
If reconciliation cannot establish the outcome, transition to
`MANUAL_REVIEW_REQUIRED`; never blindly retry an unreconcilable mutation.
Idempotency keys reduce duplicate work but do not provide exactly-once external
side effects.

## Persistence and concurrency assumptions

The first durable deployment assumes one CPTR worker owns orchestration
execution against SQLite. SQLite transactions must remain short and explicit.
If multiple workers or a second database backend are introduced, leases,
locking, and migration semantics require a separate design review.

Future workspace mutation leases must identify the workspace, logical
operation, physical attempt, owner, expiry, and heartbeat. An expired lease
does not imply safe retry: orphaned work is reconciled first and may require
manual review.

### Lease epoch and RecoveryLease

Each workspace mutation lease carries a monotonically increasing `epoch`
(fencing token) for its logical operation. Every acquisition or recovery
increments the epoch. A mutating adapter and its verifier must carry the
epoch, and a stale writer whose epoch is no longer current must be rejected.
Lease expiry alone never authorizes a stale writer to continue.

`RecoveryLease` is a separate exclusive contract for recovering one stale run.
It contains the run ID, recovery owner, lease epoch, acquired-at time, expiry,
heartbeat, and purpose. Only one worker may hold a `RecoveryLease` for a run;
normal execution cannot use one as a mutation lease. Recovery first reconciles
the prior physical attempt, then either records a positively verified outcome
or transitions the operation to `MANUAL_REVIEW_REQUIRED`.

## Ports and trust boundaries

- `SubagentExecutionPort`: delegates to the existing CPTR execution primitive;
  it does not call a provider directly.
- `ToolFacade`: exposes only CPTR tools allowed by the capability and approval
  policy; it cannot silently bypass CPTR approval.
- `ApprovalPort`: adapts existing CPTR approval authority rather than
  reimplementing approval decisions.
- Evidence/verifier boundary: evidence is untrusted input until a verifier
  checks source, scope, freshness, and expected operation identity.
- Gate-deciding authoritative evidence must be verifier-generated or directly
  observed by the trusted runtime. A specialist self-report, adapter claim, or
  copied log is untrusted input and cannot by itself authorize a transition,
  success, retry, or recovery decision.
- External adapters: return structured outcomes and evidence; they do not
  decide retries or bypass `MANUAL_REVIEW_REQUIRED`.

## Governance and security

Strict governance maps `UNKNOWN` to `DENY`. High-risk capabilities such as
command execution, file mutation, Git mutation, MCP, and network access are
contracts only until a later phase grants them through explicit policy and
approval. Shell parsing is not a complete security boundary; workspace,
identity, adapter, and approval boundaries must all enforce policy.

The coordinator (Heidi) may classify, choose strategy, construct a specialist
graph, and synthesize closure evidence. Specialists cannot delegate in the
initial graph. Coordination remains separate from CPTR execution ownership.

## Event, recovery, and review requirements

Future orchestration events need versioned schemas, correlation IDs, replay
semantics, and an explicit distinction between diagnostic/shadow events and
authoritative CPTR `events:chat` output. Recovery must be evidence-driven and
must surface stale leases, unknown outcomes, and manual-review states instead
of reporting false success.

## Threat model summary

Threats include prompt-driven capability escalation, confused-deputy identity,
workspace escape, malicious tool output, provider/adaptor compromise,
duplicate external side effects, stale leases, event replay gaps, and
diagnostic leakage. Mitigations are least-privilege capability grants,
identity-bound workspace checks, explicit approvals, bounded redacted evidence,
operation/attempt identity, reconciliation before retry, lease heartbeats,
versioned event contracts, and fail-closed governance.