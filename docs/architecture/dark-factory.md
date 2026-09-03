# CPTR Dark Software Factory Architecture

## Status

Design target for the durable autonomous software-engineering factory hosted by `computer` / CPTR. The companion `chatgpt-computer-plugin` remains a thin authenticated MCP adapter. `chatgpt-terminal-plugin` is an optional execution provider, not the factory controller.

This design extends CPTR's existing control plane, durable autonomous supervisor, direct coding workers, verification, evidence, approvals, Skills, FDX, MCP client, browser/web providers, Git services, runtime metrics, and SQLite/Alembic persistence. It deliberately does not introduce a second task engine, second database, or competing execution lifecycle.

## Objectives

The factory must accept an immutable mission and machine-checkable acceptance criteria, then autonomously drive a durable engineering loop:

```text
MISSION
  -> RECOVER
  -> BASELINE
  -> UNDERSTAND
  -> AUDIT
  -> SELECT_FINDING
  -> CAPABILITY_ANALYSIS
  -> SKILL_DISCOVERY
  -> TRUST_EVALUATION
  -> SKILL_SELECTION
  -> REPRODUCE
  -> ROOT_CAUSE
  -> PLAN
  -> IMPLEMENT
  -> TARGETED_VERIFY
  -> FULL_VERIFY
  -> ADVERSARIAL_REVIEW
  -> SECURITY_REVIEW
  -> LIVE_VERIFY
  -> VICTORY_JUDGE
       | FAIL -> REPAIR_REQUIRED -> diagnose -> repair -> reverify
       | PASS
       v
     COMMIT
       -> PUSH
       -> CI_VERIFY
            | FAIL -> diagnose -> repair
            | PASS -> CYCLE_COMPLETE -> NEXT_AUDIT
```

The system must survive ChatGPT/MCP disconnects and CPTR restarts, retain objective evidence, prevent duplicate workers/commits after recovery, learn which capabilities work for which task families, and fail closed when required verification is missing.

## Non-goals

The first implementation does not:

- replace CPTR's existing `AgentService`, terminal/command manager, direct coding worker service, browser stack, FDX integration, MCP client, or Git layer;
- move orchestration into `chatgpt-computer-plugin`;
- treat a language model as a trusted source of success state;
- automatically execute arbitrary code or instructions downloaded from the Internet;
- make host-level isolation claims stronger than CPTR's actual deployment boundary;
- require every reasoning role for every task;
- require every repository to have the same test/build commands;
- make asynchronous chat subagents durable. Factory reasoning-role durability is owned by factory records, not `cptr.utils.async_subagents`.

## Existing CPTR foundations reused

The repository audit at the start of this implementation found these reusable foundations on `computer/main`:

- `AutonomousMonitor`, `AutonomousScope`, `AutonomousEvidence`, `AutonomousApproval`, and `AutonomousWorkspaceLease` persist autonomous control-plane state.
- `SqlSupervisorStore` already implements idempotent monitor creation, atomic monitor leases, workspace writer leases, durable evidence, approvals, and active-run recovery queries.
- `AutonomousSupervisor` already owns bounded retry escalation, deterministic worker-task idempotency keys, approval pauses, independent verification before director evaluation, same-worker steering evidence, and final-gate repair loops.
- `AgentService` is the shared durable boundary over CPTR's existing model/worker lifecycle.
- `DirectCodingWorkerService` creates clean branch-backed Git worktrees and prevents dirty-base creation and overlapping integration.
- `DefaultIndependentVerifier` runs fixed-argv commands without shell interpolation and captures bounded command evidence.
- `OpenAISupervisorDirector` already isolates provider-specific Responses API calls behind a director protocol and persists continuation response IDs through monitor state.
- `cptr.utils.skills` already discovers repository/global Agentskills-compatible skills and supports progressive disclosure.
- `MCPClient` already supports Streamable HTTP and stdio MCP servers.
- FDX, browser/web search, Git, LSP, command sessions, runtime metrics, and durable MCP usage/benchmark telemetry already exist.
- startup calls `recover_monitors(app)` after database initialization, establishing the existing recovery pattern.

The factory builds on these abstractions rather than duplicating them.

## Architectural ownership

```text
GPT-5.6 Sol
  reasoning / architecture / research / debugging / review / judging
        |
chatgpt-computer-plugin
  thin MCP schemas + auth forwarding + presentation only
        |
computer / CPTR
  DARK FACTORY RUNTIME
    - durable runs/cycles/steps
    - state transition authority
    - capability intelligence
    - trust/quarantine
    - reasoning-role orchestration
    - worker/worktree allocation
    - evidence + verification
    - approvals + budgets
    - recovery + idempotency
    - Victory decisions
        |
execution capability providers
    - CPTR coding/files/commands
    - DirectCodingWorker worktrees
    - Git / FDX / LSP
    - browser + web providers
    - MCP servers
    - installed skills
    - SSH where policy permits
    - optional chatgpt-terminal-plugin remote agents
```

The backend is server-authoritative. MCP clients can request actions and read status/evidence but cannot mutate internal factory state directly.

## Trust boundaries

### Trusted for state authority

- CPTR database transactions and validated domain transitions.
- Server-owned verification code.
- Exit codes, structured tool results, repository fingerprints, Git state, CI provider responses, benchmark measurements, and other machine-observed evidence after provenance validation.
- Explicit user approvals authenticated by CPTR.

### Untrusted or advisory

- implementer prose;
- verifier-model prose;
- external documentation text;
- GitHub README instructions;
- downloaded skills, scripts, packages, MCP manifests, and repositories;
- web-search results;
- tool descriptions supplied by third-party MCP servers;
- generated patches before independent verification.

Untrusted information can inform planning. It cannot by itself authorize execution, widen permissions, mark a gate passed, or set Victory.

## Threat model

The factory must defend against:

1. **False success** — a worker claims tests passed when they did not.
2. **Prompt injection** — external docs/skills try to redirect the factory, exfiltrate secrets, disable checks, or broaden scope.
3. **Supply-chain compromise** — a discovered package/skill/MCP server is malicious or changes after evaluation.
4. **Permission escalation** — a capability requests file/network/credential/deploy access beyond the mission.
5. **Duplicate mutation** — restart or concurrent recovery starts the same mutation twice or commits twice.
6. **Cross-workspace mutation** — a worker escapes its owned repository/worktree.
7. **Stale evidence** — a gate result from an earlier revision is reused after the code changes.
8. **Self-review bias** — one reasoning context implements and unilaterally validates its own work.
9. **Unbounded resource use** — queues, logs, retries, research, contexts, workers, or tool output grow without limits.
10. **Approval bypass** — a push/deploy/destructive/external operation executes without the required authenticated approval.
11. **CI cargo-cult retrying** — failing CI is rerun repeatedly without diagnosis.
12. **Acceptance erosion** — criteria are silently dropped or converted to non-applicable to obtain success.

## Core domain

### FactoryRun

One durable mission. Required fields:

- stable `factory_run_id`;
- owner/user and workspace IDs;
- immutable mission text;
- immutable acceptance criteria;
- selected model/director policy;
- current state;
- current cycle;
- budget/policy snapshot;
- pause/approval/block/failure metadata;
- lease token/expiry;
- timestamps;
- durable next action;
- versioned configuration fingerprint.

A deterministic user-scoped idempotency key prevents duplicate run creation.

### FactoryCycle

One coherent engineering improvement within a run. It stores:

- ordinal and lifecycle state;
- selected finding and priority rationale;
- baseline repository revision/fingerprint;
- capability requirements;
- selected capability set;
- repair attempt counters and normalized failure signatures;
- mutation worker/worktree identity;
- target revision;
- next action;
- cycle timestamps.

A cycle is not equivalent to Victory. A cycle can finish only after its applicable verification, commit/push/CI policy, and evidence requirements are satisfied.

### FactoryEvent

Immutable ordered transition/audit event:

- run/cycle IDs;
- monotonic per-run sequence;
- idempotency key where the action is retryable;
- event type;
- actor class (`system`, `user`, `reasoning_role`, `worker`, `verifier`, `ci`);
- previous and next state where applicable;
- bounded redacted payload;
- timestamp.

Events are the replay/audit timeline, not the only persistence source. Current run/cycle projections are stored separately for efficient recovery.

### FactoryEvidence

Immutable evidence record:

- run/cycle/gate/step linkage;
- evidence kind and source;
- bounded redacted structured payload;
- content digest;
- repository revision/workspace fingerprint when relevant;
- creation time;
- optional expiry/staleness policy.

Evidence that can prove a gate must be revision-bound so a subsequent mutation invalidates it.

### FactoryGateResult

Machine-owned gate projection:

- gate ID/category;
- required flag;
- applicability policy and resolved applicability;
- status (`PENDING`, `PASS`, `FAIL`, `NOT_APPLICABLE`);
- evidence IDs;
- command/tool provenance;
- evaluated repository revision/fingerprint;
- reason;
- attempt;
- timestamps.

A required gate can be `NOT_APPLICABLE` only when its declarative applicability rule evaluates false and the reason/evidence is persisted. A worker or model cannot simply omit a gate.

## Explicit factory state machine

The state machine is a backend domain contract, not a prompt convention.

```text
MISSION
RECOVERING
BASELINING
UNDERSTANDING
AUDITING
SELECTING_FINDING
CAPABILITY_ANALYSIS
SKILL_DISCOVERY
TRUST_EVALUATION
SKILL_SELECTION
REPRODUCING
ROOT_CAUSE_ANALYSIS
PLANNING
IMPLEMENTING
TARGETED_VERIFYING
FULL_VERIFYING
ADVERSARIAL_REVIEW
SECURITY_REVIEW
LIVE_VERIFYING
VICTORY_JUDGING
REPAIR_REQUIRED
COMMITTING
PUSHING
CI_VERIFYING
CYCLE_COMPLETE
PAUSED
APPROVAL_REQUIRED
BLOCKED
FAILED
COMPLETE
CANCELLED
```

Normal forward transitions follow the loop order. Explicit alternate transitions include:

- any active state -> `PAUSED`;
- any risky-operation boundary -> `APPROVAL_REQUIRED`;
- `PAUSED` -> the persisted resumable prior state;
- `APPROVAL_REQUIRED` -> persisted resumable prior state on approval, or `BLOCKED` on denial where work cannot continue safely;
- verification/review/judge/CI failure -> `REPAIR_REQUIRED`;
- `REPAIR_REQUIRED` -> `ROOT_CAUSE_ANALYSIS`, `CAPABILITY_ANALYSIS`, or `IMPLEMENTING` according to classified failure and retry policy;
- `CYCLE_COMPLETE` -> `AUDITING` for another useful cycle or `COMPLETE` only through the run completion policy;
- unrecoverable system failure -> `FAILED`;
- policy/attempt exhaustion -> `BLOCKED`;
- user stop after owned execution quiesces -> `CANCELLED`.

Invalid transitions fail closed and are never silently coerced.

### Transition authority

A transition request contains actor class, idempotency key, reason, and required evidence references. Domain code validates the graph and transition-specific preconditions. `COMPLETE` and the post-Victory path are reserved system transitions. `reasoning_role` and `worker` actors are explicitly forbidden from setting `COMPLETE`, `CYCLE_COMPLETE`, or a gate status directly.

## Baseline and repository understanding

Before mutation the factory records:

- repository root and authorization boundary;
- current branch/revision/upstream;
- dirty state and whether user work exists;
- repository language/framework/toolchain inventory;
- project-defined test/lint/typecheck/build commands where discoverable;
- dependency manifests and lockfiles;
- CI workflows;
- deployment/runtime surfaces where relevant;
- FDX/index capability status;
- bounded repository fingerprint.

Dirty user work is never automatically discarded. Mutation is routed into an isolated Direct Coding Worker worktree rooted at a clean reviewed base revision.

## Audit and finding selection

Audit providers may run read-only work in parallel. Findings are normalized records containing severity, confidence, affected area, reproduction evidence, likely impact, estimated effort, dependencies, and acceptance mapping.

Selection is server-visible and deterministic enough to audit. The reasoning director can rank candidates, but the selected finding is persisted with its rationale and cannot expand the immutable mission scope without user approval.

## Reasoning architecture

### Logical roles

The orchestration supports independent contexts:

- **Architect Sol** — architecture, invariants, high-risk planning.
- **Research Sol** — external documentation/research synthesis.
- **Skill Judge Sol** — capability requirements, trust-aware candidate comparison.
- **Implementer Sol** — bounded implementation task.
- **Debugger Sol** — independent failure diagnosis.
- **Adversarial Sol** — attempts to invalidate claimed success and find bypasses.
- **Security Sol** — threat/supply-chain/permission review.
- **Verifier Sol** — interprets machine evidence and identifies missing proof; it cannot fabricate gate results.
- **Victory Judge Sol** — advisory synthesis over already machine-evaluated gates; it cannot override a failed/missing required gate.

Roles are logical isolation boundaries. They may share an underlying provider/model but must use separate response/conversation state when independence matters.

### Adaptive reasoning expenditure

Trivial deterministic work uses fewer roles. The strongest configured reasoning is reserved for ambiguous root causes, architecture, security-sensitive changes, repeated failure, adversarial review, and final high-risk judgment. Budgets cap per-role turns, tokens, wall-clock runtime, external requests, and repair attempts.

### Structured outputs

Reasoning outputs are schema-validated and bounded. They contain decisions, findings, hypotheses, capability requirements, plans, or critiques—not hidden chain-of-thought. Invalid structured output is a recoverable provider failure, not implicit permission to continue.

## Capability and Skill Intelligence

### Capability analysis

Before unfamiliar/high-risk work, the factory produces a capability requirement set such as:

- repository semantic analysis;
- TypeScript/React expertise;
- Python concurrency debugging;
- protocol conformance;
- browser/live runtime inspection;
- security review;
- Git/CI operations;
- deployment diagnostics.

It then inventories local trusted capabilities before considering external discovery.

### Discovery sources

Supported provider classes:

1. repository-local skills;
2. CPTR/global installed skills;
3. built-in CPTR tools and services;
4. configured MCP servers/registries;
5. official vendor/library documentation;
6. GitHub repositories/releases;
7. package ecosystems;
8. reputable engineering/research sources;
9. broader web search when a research trigger fires.

Discovery providers return normalized candidates and raw source references. They do not execute candidates.

### Normalized capability manifest

Each candidate has at minimum:

```text
stable_id
version
origin_type
origin_uri/source
pinned_version_or_commit
digest
capabilities[]
permissions[]
network_requirements[]
execution_requirements[]
risk_classification
trust_status
verification_status
maintenance metadata
historical_factory_score
created_at / evaluated_at
```

Trust states are explicit, for example:

```text
DISCOVERED
FETCHED
PINNED
QUARANTINED
REJECTED
APPROVED
REVOKED
STALE_REVIEW_REQUIRED
```

### Candidate scoring

Security/trust is a hard gate. Only candidates that pass the applicable trust policy can enter ranking.

Ranking then considers:

- task fit;
- verified quality/capability test;
- provenance quality;
- historical verified success;
- maintenance health;
- freshness relative to the target API/framework;
- latency;
- token efficiency;
- runtime/resource efficiency;
- cost.

Scores are decomposed and persisted so selection is explainable. Historical performance never overrides a failed current trust check.

## External skill quarantine pipeline

```text
DISCOVER
  -> FETCH into non-executable quarantine
  -> PIN immutable version/commit
  -> HASH contents
  -> PROVENANCE_CHECK
  -> MANIFEST/PERMISSION_ANALYSIS
  -> STATIC_SECURITY_AUDIT
  -> DEPENDENCY/SUPPLY_CHAIN_AUDIT
  -> PROMPT_INJECTION_AUDIT
  -> QUARANTINE_EVALUATION
  -> CAPABILITY_TEST in constrained environment
  -> APPROVED_CACHE
```

### Security rules

- Network-fetched content is data until approved.
- Instructions inside fetched content cannot change factory policy, acceptance criteria, permissions, or approval requirements.
- Candidate scripts are not executed during provenance/static analysis.
- Pinning and digest verification occur before capability testing.
- Requested filesystem/network/process/credential permissions are compared with mission policy; excess permissions disqualify or require explicit user approval depending on risk class.
- Dependency manifests are inspected before installation. Unpinned install instructions are not executed blindly.
- Cache entries are keyed by immutable identity + digest; a changed version is a new candidate and requires re-evaluation.
- Revocation/staleness can remove a previously approved candidate from routing.

## Skill performance memory

Persist empirical effectiveness by immutable capability version and normalized task/repository family:

```text
capability_id
capability_version
task_family
repository_family
language_family
attempt_count
verified_success_count
failure_count
regression_count
repair_iterations_total / median
input_tokens / output_tokens
runtime_ms
cost_pico_usd
confidence
last_evaluated_version
last_successful_version
last_used_at
```

Updates occur from factory outcome evidence, not model ratings. A confidence calculation must account for sample count and recency. Performance rows influence future ranking only after trust eligibility.

## Research triggers

Expanded external research is not the default. Trigger it when one or more persisted signals occur:

- confidence below configured threshold;
- unfamiliar framework/protocol/library;
- repeated normalized repair signature;
- local capability historical success below threshold;
- security-sensitive code touched;
- current API/documentation uncertainty;
- unexplained benchmark/performance regression;
- high disagreement between independent reasoning roles;
- verifier evidence contradicts implementation assumptions.

Research has bounded provider count, results, bytes, tokens, and wall-clock budget.

## Worker execution architecture

### Mutation isolation

Mutating implementation runs inside `DirectCodingWorkerService` worktrees whenever the repository is Git-backed and a clean base is available. Factory records own the worker ID, branch, base revision, and expected change scope.

Read-only investigations can run concurrently. Mutations are serialized per owned change scope unless a plan proves disjoint path ownership. Integration uses CPTR's existing non-overlap checks; no worker may overwrite dirty base paths.

### Capability router

The worker-facing capability router resolves an approved capability manifest to an existing provider:

- CPTR direct file/coding primitives;
- command/PTY session;
- FDX;
- LSP;
- browser/web;
- MCP client;
- SSH provider where configured and approved;
- optional `chatgpt-terminal-plugin` remote-agent provider.

Provider-specific mechanics stay behind adapters. Factory state stores capability identity and evidence, not transport-specific transient handles as authoritative state.

### Cleanup

Every worker/process/browser/MCP/remote execution has an owner, bounded lifetime, cancellation path, and cleanup evidence. Factory cancellation does not become `CANCELLED` until owned mutation execution is quiescent or the run is explicitly `BLOCKED` because quiescence could not be proven.

## Verification architecture

### Declarative gates

Repository policy resolves a gate plan at baseline. Supported categories include:

```text
acceptance
reproduction
regression
focused_tests
broader_tests
unit
integration
e2e
typecheck
lint
build
security
isolation
resource
performance
cleanup_lifecycle
adversarial
git_diff_review
git_diff_check
ci
runtime_smoke
live_verify
```

Repositories do not need every category. Applicability is explicit. A required gate cannot silently vanish because a command is unavailable; that becomes `BLOCKED` or requires a documented applicability resolution.

### Evidence rules

A passing gate requires concrete evidence appropriate to the gate, for example:

- argv + exit code + bounded output + duration;
- test report/counts;
- structured FDX/LSP result;
- Git diff/check/status result;
- CI run/check identity and conclusion;
- runtime HTTP/MCP operation result;
- resource measurements before/after;
- security scanner/static analysis findings;
- adversarial test result.

Evidence is revision/fingerprint bound. Any mutation after verification invalidates affected gate results and returns them to `PENDING`.

### Victory engine

`FactoryVictoryJudge` is deterministic and machine-owned. It evaluates the stored gate plan and gate evidence. It returns PASS only when:

1. every applicable required gate is `PASS`;
2. every required non-applicable gate has an explicit valid applicability resolution;
3. no applicable gate is `FAIL` or `PENDING`;
4. every `PASS` references accepted evidence bound to the current revision/fingerprint;
5. acceptance criteria retain their immutable identity and all are covered;
6. no unresolved blocking security/adversarial finding exists;
7. the repository diff itself passes required integrity checks;
8. required CI/live gates are current when the run policy demands them.

A model cannot call a setter that marks Victory. The advisory Victory Judge Sol may explain or challenge evidence, but its approval cannot override machine gate failure. Conversely, model disagreement cannot turn a fully machine-satisfied gate set into success if a required human approval remains pending.

### False-positive resistance

Tests must explicitly attempt to:

- supply worker prose saying all tests passed while a required gate failed;
- omit a required gate;
- mark a required gate non-applicable without reason;
- reuse evidence from an older revision;
- make a reasoning-role actor request `COMPLETE`;
- replay an idempotency key with different transition intent;
- approve the wrong/stale approval record.

All must fail closed.

## Failure and repair loop

When a required gate fails:

1. persist exact failure evidence;
2. normalize a stable failure signature;
3. increment bounded signature and cycle attempt counters;
4. classify cause (`implementation`, `test`, `environment`, `capability`, `security`, `ci`, `runtime`, `unknown`);
5. invoke an independent Debugger Sol when useful;
6. re-enter capability analysis/research if evidence says the current capability set is insufficient;
7. transition to the smallest justified repair phase;
8. mutate in the owned worktree;
9. invalidate stale affected gate evidence;
10. rerun targeted verification, then all required final gates.

Repeated identical failures escalate strategy. CI is diagnosed before rerun. Acceptance criteria and security policy are immutable during repair unless the authenticated user explicitly changes the mission through a versioned run amendment.

## Approvals

Reuse CPTR's authenticated approval concepts. Factory approval records include operation class, exact bounded action, reason, capability/provider, requested permissions, target, expiry, and resumable prior state.

Approval-required examples:

- push to external Git remote when deployment policy requires it;
- production deployment/release;
- destructive data/storage operation;
- credential rotation/access;
- paid/costly external action above budget threshold;
- enabling a quarantined capability requiring permissions outside current policy.

Approval is not a blanket bypass. It authorizes the recorded action/permission envelope only.

## Budgets

Per-run policy supports bounded:

- total wall-clock runtime;
- cycle count;
- repair attempts per normalized failure;
- concurrent read-only investigations;
- concurrent mutators;
- reasoning requests and token budget by role;
- external research requests/results/bytes;
- network calls;
- package install operations;
- command runtime/output;
- monetary/API-equivalent cost where measurable.

Budget exhaustion transitions to `BLOCKED` with evidence unless the user explicitly raises the budget.

## Cost and token accounting

Reuse the durable MCP usage/engineering metrics work where applicable. Factory-specific reasoning calls and capability executions attach usage metrics to run/cycle/role/capability IDs. Report actual provider usage when available and clearly labeled estimates otherwise. Do not fabricate hidden ChatGPT billing information.

## Persistence and database ownership

SQLite/Alembic remains the authoritative persistence system for the single-host CPTR runtime. Initial factory tables are additive and normalized around runs, cycles, events, evidence, and gate results. Later phases add capability manifests/evaluations/performance and reasoning-role attempts.

No in-memory object is the sole source of truth for resumable factory progress. In-memory tasks are execution accelerators only.

## Restart recovery

Startup recovery follows the existing monitor pattern:

1. initialize DB/migrations;
2. query non-terminal factory runs;
3. reclaim expired run leases atomically;
4. inspect the persisted current state and owned execution references;
5. reconcile worker/worktree/command/CI state;
6. replay no side effect without an idempotency key;
7. resume from the persisted next action or move to `RECOVERING`/`REPAIR_REQUIRED` when evidence is incomplete.

Recovery must never infer success from the prior process disappearing. Missing transient execution becomes evidence to diagnose.

## Idempotency and concurrency

- Run creation: user-scoped deterministic idempotency key.
- Transitions/actions: run-scoped idempotency key bound to operation payload digest.
- Worker attempts: deterministic run/cycle/attempt identity, mapped to existing task/direct-worker idempotency where available.
- Evidence ingest: stable evidence digest/source identity prevents duplicate replay records where required.
- Commit: cycle/revision-bound commit intent; recovery checks Git history before creating another commit.
- Push/CI: remote revision/check identity persisted before polling.

One process owns a run lease at a time. Read-only subwork may be parallel. Writer ownership is coordinated with existing workspace/direct-worker boundaries, not an unbounded global lock.

## Git and PR lifecycle

Default mutation flow:

1. capture clean reviewed base revision;
2. create isolated Direct Coding Worker branch/worktree;
3. implement verified coherent chunk;
4. inspect actual diff and changed-path manifest;
5. `git diff --check`;
6. run required gates;
7. commit with deterministic cycle metadata only after machine gate success for the chunk;
8. push only when policy/approval allows;
9. update the same implementation PR;
10. persist PR/head/base identity;
11. monitor CI via provider status, not sleeps;
12. diagnose failed checks before repair/rerun.

The factory never merges by itself unless an explicit repository/user policy grants that action. For this project, merge remains user-approved.

## CI lifecycle

CI tracking stores provider, repository, commit SHA, run/check IDs, status/conclusion, URLs/opaque IDs where safe, timestamps, and bounded failure summaries. Polling is bounded and exits early on failure. No arbitrary fixed sleep is used as a correctness mechanism. Provider-specific waiting can use event/webhook integration or bounded status polling.

A new local mutation invalidates prior CI success because the revision changed.

## Observability

Expose bounded metrics/events for:

- active/paused/blocked runs;
- state dwell time;
- cycle throughput;
- retry/failure signatures;
- gate pass/fail latency;
- reasoning role calls/tokens/runtime;
- capability selection and success rates;
- quarantine outcomes;
- external research volume;
- worktree/command/browser counts;
- event queue/backpressure;
- CI latency;
- Victory false-positive prevention counters;
- process RSS/open-FD/event-loop metrics via existing runtime metrics.

No source code, secrets, raw prompts, or hidden reasoning is required in operational metrics.

## MCP/control API

The backend exposes a compact factory surface under the authenticated control API. The plugin maps these to ChatGPT tools:

```text
POST /api/control/v1/factory/runs                       -> cptr_factory_start
GET  /api/control/v1/factory/runs/{run_id}              -> cptr_factory_status
GET  /api/control/v1/factory/runs/{run_id}/events       -> cptr_factory_events
GET  /api/control/v1/factory/runs/{run_id}/evidence     -> cptr_factory_evidence
POST /api/control/v1/factory/runs/{run_id}/messages     -> cptr_factory_message
POST /api/control/v1/factory/runs/{run_id}/pause        -> cptr_factory_pause
POST /api/control/v1/factory/runs/{run_id}/resume       -> cptr_factory_resume
POST /api/control/v1/factory/runs/{run_id}/approve      -> cptr_factory_approve
POST /api/control/v1/factory/runs/{run_id}/stop         -> cptr_factory_stop
```

The MCP adapter contains no factory transition graph, skill trust logic, Victory logic, or durable scheduler.

## API response principles

Status returns projections: current state, cycle, current/next action, progress, pending approval, budgets, selected capabilities, and latest gate summary. Events/evidence are cursor-paginated and bounded. Sensitive host paths, credentials, raw external content, and hidden model reasoning are redacted.

## Deployment and migration

Factory schema changes use additive Alembic migrations. New code must tolerate runs created by older compatible schema versions through explicit version fields/migrations. Deployment order:

1. migrate DB;
2. deploy backend capable of reading new records;
3. enable factory feature flag/control routes;
4. deploy thin plugin adapter;
5. verify health, factory start/status/evidence, restart recovery, and live event flow;
6. expand capability/research providers after core Victory/recovery invariants are proven.

## Rollback

Application rollback must not destroy factory evidence. When code is rolled back to a version that cannot safely resume a newer factory schema/state, runs remain durable and should be paused/blocked rather than coerced into older semantics. Database downgrade is a deliberate operator action and is not part of normal application rollback.

A production rollback candidate must understand the database's current Alembic head. The Phase 10 rollback exercise therefore rolls application code back only to a previously verified revision that already contains migration `0025`, proves health/recovery, then redeploys the target revision. Rolling back to an application whose migration graph cannot resolve the current database revision is intentionally rejected rather than treated as a valid rollback test.

## Phase 10 qualification evidence

The 2026-09-03 production-hardening campaign exercised actual CPTR process restart against isolated durable databases at nine boundaries: run creation, mutation-worker creation, mutation evidence, machine verification PASS, machine Victory authorization, commit intent, commit observation, push observation, and CI observation. Restart recovery preserved each marker and created exactly one `RECOVERING` transition. Concurrent recovery owners produced one lease winner.

External-content adversarial cases used malicious skill, official-document, and MCP-server descriptions that attempted to override trust, enable network access, and request secrets. Trust evaluation rejected the candidates and left the immutable policy fingerprint unchanged. The Victory campaign separately rejected failed, missing, stale-revision, advisory-only, and unresolved-security cases.

A bounded concurrency/pressure campaign executed 96 concurrent factory operations with zero database errors and zero SQLite busy events. Maximum measured event-loop lag was 27.628 ms; RSS increased by 3,612,672 bytes; open-FD delta was zero; all 64 completed command handles were reaped; slow live subscribers were disconnected under pressure and subscriber count returned to zero after closure.

Local release gates reached 452/452 Python tests, 48/48 frontend tests plus a clean production build, and 138/138 companion-plugin tests after merging current plugin `main`; plugin TypeScript typecheck/build passed with a 169,993-byte Workbench bundle under its 450,000-byte limit. Fresh and prior-compatible schema migrations both reached `0025`. Repository-wide Ruff lint passes and all Phase 9/10 Python changes are Ruff-format clean. The wider repository still has pre-existing global Ruff-format and Svelte typecheck debt; Phase 10 records that debt explicitly instead of modifying unrelated files to manufacture a green global baseline.

## Test strategy

### Domain/unit

- complete state-transition matrix;
- forbidden worker/model terminal transitions;
- idempotency replay and payload mismatch;
- gate applicability and required-gate omission;
- Victory false-positive attacks;
- stale revision evidence rejection;
- failure signature escalation;
- capability score decomposition;
- trust hard-gate behavior;
- permission and injection detection.

### Persistence/recovery

- run/cycle/evidence/gate round trip;
- unique sequence/idempotency races;
- lease expiry/reclaim;
- process restart at every mutation boundary;
- restart between worker creation and state persistence;
- restart between verified commit intent and commit/push observation;
- no duplicate worker/commit after restart.

### Integration

- Direct Coding Worker creation/integration;
- command verification evidence;
- FDX/LSP/browser provider routing;
- local and external skill discovery;
- quarantine/cache behavior;
- MCP server candidate inventory;
- approval lifecycle;
- CI lifecycle.

### Adversarial/security

- malicious skill instructions requesting secrets or policy changes;
- path traversal/symlink escape attempts;
- changed remote content after pinning;
- dependency/manifest permission escalation;
- fake success prose;
- forged/stale evidence references;
- cross-run approval replay;
- cross-workspace worker references.

### End-to-end/live

- start a factory mission through the MCP adapter;
- disconnect ChatGPT, restart adapter, observe backend continuation;
- restart CPTR mid-cycle, verify exact recovery;
- inject one deterministic defect, prove reproduce -> repair -> gate -> commit flow;
- force one gate failure and prove Victory remains impossible;
- verify only one coherent PR lifecycle when configured.

## Performance and resource constraints

- all event/evidence/list APIs are paginated/bounded;
- command and external-source content is truncated before persistence/LLM use;
- capability catalogs use progressive disclosure;
- research result count and bytes are capped;
- concurrent read-only investigations and workers are bounded;
- mutation workers are bounded by existing Direct Coding Worker limits;
- no unbounded queues or retained reasoning contexts;
- DB indexes cover active-run recovery, run/cycle lookup, event sequence, evidence/gate lookup, and capability performance routing;
- verification should parallelize independent read-only gates only when resource policy allows and deterministic output ordering is retained.

## Security invariants

1. Authentication/ownership are checked in CPTR, not trusted from MCP metadata.
2. External content cannot alter policy or gate authority.
3. Security trust eligibility is evaluated before candidate ranking.
4. No secret is stored in a capability manifest or evidence payload.
5. Mutating paths stay within authorized worktree/repository boundaries.
6. Network/package/deploy/destructive actions require explicit policy and, where configured, approval.
7. Worker/model prose never sets a gate result or Victory.
8. Required gates cannot disappear silently.
9. Evidence is revision-bound and redacted.
10. Recovery replays decisions, not side effects without idempotency.

## Implementation phases

### Phase 1 — Durable factory domain

Add factory run/cycle/event/evidence/gate persistence, explicit state-transition validation, idempotent create/transition operations, leases, and recovery primitives.

### Phase 2 — Victory and verification engine

Add declarative gate plans, revision-bound evidence, deterministic Victory evaluation, stale evidence invalidation, and false-positive/adversarial tests.

### Phase 3 — Sol orchestration

Add provider-neutral role calls, separate role response state, structured output schemas, budgets, retry/backoff policy, and advisory Victory/security/adversarial roles.

### Phase 4 — Skill Intelligence

Normalize local CPTR/skills/MCP/tool capabilities, trust states, ranking, task-family routing, and empirical performance persistence.

### Phase 5 — External discovery/quarantine

Add official-doc/GitHub/package/MCP discovery providers, pin/hash/provenance/permission/static/dependency/injection analysis, quarantine capability tests, and immutable approved cache.

### Phase 6 — Worker execution

Bind cycles to Direct Coding Worker worktrees, bounded parallel read-only analysis, mutation ownership, capability routing, cleanup, and restart reconciliation.

### Phase 7 — Full dark loop

Implement baseline/audit/finding/capability/reproduce/root-cause/plan/implement/verify/review/repair/commit/CI cycle scheduling.

### Phase 8 — ChatGPT MCP surface

Expose the compact control API and adapter tools plus status/events/evidence/approval UI integration.

### Phase 9 — Benchmarking and learning

Connect real-work metrics, capability performance learning, cost/token accounting, and standardized isolated benchmark regression reporting.

### Phase 10 — Production hardening

Run restart/failure-injection/concurrency/security/soak campaigns, deploy, rollback-test, and perform live ChatGPT verification.

## Initial implementation slice

The first production slice deliberately implements Phases 1 and the machine-authoritative core of Phase 2 before any external skill execution. This is the minimum safe foundation: without a durable state machine and fail-closed Victory engine, adding autonomous Internet capability discovery would increase risk faster than capability.

The slice must prove:

- explicit transitions reject invalid paths;
- a reasoning role/worker cannot set terminal success;
- run creation and transition replay are idempotent;
- runs/cycles/events/evidence/gates survive DB reload;
- run leases are atomically claimable and recoverable;
- required failed/pending/missing/stale gates prevent Victory;
- required non-applicable gates require explicit applicability evidence;
- PASS gates require accepted current-revision evidence;
- machine Victory is the only input that can authorize the success path.

## Known baseline finding

The pre-implementation targeted suite passed, but Python 3.14 emitted a shutdown-time `LiveEventStore` warning indicating a global asyncio queue can remain bound to a prior event loop across isolated async tests. This predates the Dark Factory work and is not used as a reason to weaken or skip the factory baseline. It should be repaired in a separate verified slice unless it materially interferes with factory event/recovery testing.

## Acceptance standard

The final implementation is scored on architecture, correctness, autonomy, reasoning quality, skill intelligence, security/trust, evidence/verifiability, persistence/recovery, performance/resource efficiency, and production readiness. No category is considered complete from prose alone. A score below 9/10 means the corresponding implementation area remains open.
