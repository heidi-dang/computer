# FlowDeck-C-PTR Phase 3 design freeze

This document specifies future contracts only. It is not a durable runtime and
none of the behavior below is implemented by Phase 0–2.

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

## Ports and trust boundaries

- `SubagentExecutionPort`: delegates to the existing CPTR execution primitive;
  it does not call a provider directly.
- `ToolFacade`: exposes only CPTR tools allowed by the capability and approval
  policy; it cannot silently bypass CPTR approval.
- `ApprovalPort`: adapts existing CPTR approval authority rather than
  reimplementing approval decisions.
- Evidence/verifier boundary: evidence is untrusted input until a verifier
  checks source, scope, freshness, and expected operation identity.
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