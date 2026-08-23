# Task 21 Final Authority and Recovery Audit

## Result

**PASS** — the integrated CPTR, FlowDeck, and control-plane stack has one
authoritative execution/governance path for execution-derived outcomes.

The control plane remains an authenticated projection/orchestration layer. It
does not own model execution, tool execution, workspace mutation authority,
fencing, physical retry identity, or final evidence truth.

## Authority map

| Concern | Authoritative system | Control-plane role | Verdict |
| --- | --- | --- | --- |
| Model/provider execution | CPTR native chat task loop | Requests work through the existing agent boundary | PASS |
| Tool execution | CPTR runtime/tool registry | No direct supervisor tool loop | PASS |
| Specialist dispatch | Authenticated FlowDeck gateway | Supervisor uses the CPTR worker boundary | PASS |
| Workspace ownership | Authenticated canonical `Workspace` row | Control routes re-check user ownership | PASS |
| Mutation lease/fencing | FlowDeck durable workspace lease and epoch | Supervisor metadata lock only serializes monitors | PASS |
| Logical idempotency | FlowDeck request/logical-operation identity | Control keys deduplicate control resources and map worker attempts | PASS |
| Physical retry identity | FlowDeck attempt lifecycle | Supervisor failure signatures select repair work | PASS |
| Cancellation/recovery | FlowDeck durable lifecycle | Control status follows terminal worker/evidence results | PASS |
| Evidence/completion | FlowDeck evidence validation and verifier contract | Control evidence is a projection and fail-closed adapter | PASS |
| Approval | Authenticated, operation-scoped approval | Approval permits an attempt; it never proves success | PASS |
| Control status | Control-plane monitor/task records | Projection and operational metadata only | PASS |

## Evidence trust hierarchy

Authoritative evidence is limited to trusted runtime/verifier observations
bound to the active attempt. The shared contract rejects mismatched attempts,
wrong outcomes, stale identity, missing evidence, unsupported sources, and
specialist claims.

Agent prose, supervisor prose, director decisions, model self-reports, and
approval records are non-authoritative. They can explain or route work but
cannot finalize success.

## State mapping

| FlowDeck/control observation | Control result |
| --- | --- |
| Authoritative succeeded evidence | Eligible for success after all policy gates |
| Failed evidence or failed worker | Failure/repair path |
| Cancelled worker | Cancelled; no late success |
| `OUTCOME_UNKNOWN` | Manual review/reconciliation; never success |
| `MANUAL_REVIEW_REQUIRED` | Manual review; never success |
| Missing or stale evidence | Not successful; fail closed |

## Recovery and concurrency

- FlowDeck owns workspace fencing; stale owners cannot publish terminal
  evidence.
- Control monitor locks prevent duplicate supervisor polling but do not grant
  mutation authority.
- Restart reloads durable monitor/task state and does not trust stale in-memory
  state.
- Existing successful worker tasks are reused by idempotency key.
- Cancellation stops future delegation and preserves uncertain outcomes.
- Late worker completion cannot override a durable cancellation or unknown
  outcome.

## Disabled behavior

With `CPTR_CONTROL_PLANE_ENABLED` unset or false, control routes are
unavailable and monitor recovery is not started. Native CPTR and FlowDeck
execution remain unchanged.

## Qualification evidence

- FlowDeck suite: 104 tests passing before the audit
- Full backend suite: 142 tests passing before the audit
- Task 21 adversarial authority tests cover missing evidence, forged prose,
  approval-as-success, terminal non-success states, identity mismatch, and
  disabled control-plane behavior
- Fresh migration initialization and the linear migration chain through 0008
  remain unchanged

No UI, FDX, unrestricted shell, Git mutation, MCP, network-write, deployment,
secrets, or broader-autonomy capability was enabled.