# CPTR Dark Software Factory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a durable, evidence-driven autonomous software-engineering factory in CPTR that can audit, research, select trusted capabilities, implement, independently verify, repair, commit, observe CI, recover across restarts, and reach Victory only from machine-verifiable evidence.

**Architecture:** `computer` owns the state machine, persistence, verification, capability trust, reasoning-role orchestration, workers, recovery and Victory. `chatgpt-computer-plugin` remains a compact authenticated MCP adapter. The design extends the existing autonomous supervisor, `AgentService`, Direct Coding Workers, Skills, FDX, MCP client, Git, browser/web, metrics and SQLite/Alembic infrastructure rather than creating parallel execution systems.

**Tech Stack:** Python 3.10+, FastAPI, SQLAlchemy async + SQLite/Alembic, existing CPTR AgentService/DirectCodingWorker/FDX/LSP/browser/MCP/Git services, OpenAI Responses-compatible director interface, Node/TypeScript MCP adapter, Svelte Workbench UI where needed.

**Spec:** `docs/architecture/dark-factory.md`

## Global Constraints

- `computer` is the durable factory runtime; the ChatGPT plugin is a thin adapter only.
- `chatgpt-terminal-plugin` is optional execution infrastructure, never the factory controller.
- Worker/model prose cannot set gate results, `CYCLE_COMPLETE`, or `COMPLETE`.
- Required gates cannot silently disappear; explicit applicability evidence is required.
- External content is untrusted data until it passes the trust/quarantine pipeline.
- Security/trust eligibility is a hard gate before candidate ranking.
- Preserve dirty user work; mutation uses isolated worktrees from a reviewed clean base.
- No hardcoded host paths, usernames, ports, credentials, tokens, models, or deployment-specific values.
- No arbitrary sleep-based correctness fixes or unbounded retries/queues/history/context.
- Fixed-argv command execution is preferred for verification; shell strings are not a trust boundary.
- Every coherent implementation slice follows RED -> observed failure -> minimal GREEN -> refactor -> targeted gate -> relevant full gate -> diff audit -> `git diff --check` -> commit.
- Push/deploy/destructive/costly external operations remain subject to existing CPTR scope and approval policy.
- Merge remains explicitly user-approved.

---

## Phase 1 — Durable factory domain

### Task 1: Explicit state machine and machine authority

**Files:**
- Create: `cptr/services/factory_domain.py`
- Test: `tests/test_factory_domain.py`

**Interfaces:**
- `FactoryState(str, Enum)` contains every state in the architecture spec and remains compatible with CPTR's Python 3.10 minimum.
- `FactoryActor(str, Enum)` distinguishes `SYSTEM`, `USER`, `REASONING_ROLE`, `WORKER`, `VERIFIER`, and `CI`.
- `FactoryTransition` is an immutable transition record with `from_state`, `to_state`, `actor`, `reason`, and optional evidence IDs.
- `validate_factory_transition(from_state, to_state, actor, *, resumable_state=None, machine_victory=False) -> None` raises `InvalidFactoryTransition` when the graph or authority rule is violated.
- `is_terminal_factory_state(state) -> bool` is the shared terminal-state predicate.

- [ ] Write `tests/test_factory_domain.py` first with one test per normal state-chain edge, repair-loop edge, pause/resume edge, approval edge, and terminal edge.
- [ ] Add explicit RED tests proving `WORKER` and `REASONING_ROLE` cannot transition to `CYCLE_COMPLETE` or `COMPLETE`, and `VICTORY_JUDGING -> COMMITTING` requires `machine_victory=True`.
- [ ] Run `python -m pytest tests/test_factory_domain.py -q` and confirm failure is due to missing factory domain code.
- [ ] Implement the smallest explicit enum/transition graph and authority checks needed to pass.
- [ ] Re-run the targeted test until green, then run Ruff on the new module/test.

### Task 2: Durable run/cycle/event/evidence/gate schema

**Files:**
- Create: `cptr/models/factory.py`
- Create: `cptr/migrations/versions/0019_dark_factory_core.py`
- Modify: `cptr/models/__init__.py`
- Test: `tests/test_factory_persistence.py`

**Interfaces:**
- `FactoryRun`: owner/workspace, immutable mission/criteria, current state/current cycle, policy/budget/config fingerprints, resumable state, next action, lease token/expiry, timestamps.
- `FactoryCycle`: run ID + ordinal, state/status, finding/capability selections, base/target revision, mutation worker, attempts/failure signatures, next action, timestamps.
- `FactoryEvent`: immutable per-run sequence with actor, event type, optional state edge, idempotency key + payload digest, bounded JSON payload.
- `FactoryEvidence`: run/cycle/gate identity, kind/source, revision/fingerprint binding, digest, bounded payload, timestamp.
- `FactoryGateResult`: gate ID/category, required/applicable state, status, evidence IDs, evaluated revision/fingerprint, reason, attempt, timestamps.
- DB uniqueness: `(user_id, idempotency_key)` for run creation when key is non-null; `(run_id, ordinal)` cycles; `(run_id, sequence)` events; `(run_id, idempotency_key)` transition/action replay when key is non-null.

- [ ] Write persistence tests first using temporary CPTR data directories and `init_db()` to prove model tables exist after migration, run/cycle round trip, ordered event sequence, evidence/gate round trip, and uniqueness constraints.
- [ ] Run the tests and confirm the missing schema/models are the RED failure.
- [ ] Add the additive `0019` migration and SQLAlchemy models with indexes for active-run recovery and run/cycle/event/gate lookups.
- [ ] Export factory models from `cptr.models`.
- [ ] Re-run persistence tests, then verify Alembic head can initialize a fresh database.

### Task 3: Factory store, idempotency and leases

**Files:**
- Create: `cptr/services/factory_store.py`
- Extend: `tests/test_factory_persistence.py`

**Interfaces:**
- `SqlFactoryStore.create_run(*, user_id, workspace_id, mission, acceptance_criteria, policy, budget, model_id, idempotency_key) -> FactoryRunRecord`
- `get_run(run_id, *, user_id=None) -> FactoryRunRecord | None`
- `create_cycle(run_id, *, base_revision, base_fingerprint, idempotency_key) -> FactoryCycleRecord`
- `transition(run_id, *, to_state, actor, reason, idempotency_key, evidence_ids=(), machine_victory=False) -> FactoryRunRecord`
- `append_evidence(...) -> FactoryEvidenceRecord`
- `record_gate(...) -> FactoryGateRecord`
- `list_events(run_id, *, after_sequence=0, limit=100) -> list[FactoryEventRecord]`
- `list_evidence(run_id, *, after_id=None, limit=100) -> list[FactoryEvidenceRecord]`
- `claim_run(run_id, *, lease_ms) -> bool`, `renew_run(...) -> bool`, `release_run(...) -> None`
- `list_recoverable() -> list[FactoryRunRecord]`

- [ ] Add RED tests for idempotent run creation, identical transition replay returning the original event, same idempotency key + different payload failing closed, concurrent lease claims allowing one owner, expired lease reclaim, and terminal runs excluded from recovery.
- [ ] Run the tests and record the expected failures.
- [ ] Implement transactional store operations with redaction and deterministic payload digests.
- [ ] Ensure transition validation delegates to `factory_domain` and transactionally writes the current projection plus immutable event.
- [ ] Re-run persistence tests and a small concurrent claim stress test.

### Task 4: Restart recovery primitive

**Files:**
- Create: `cptr/services/factory_runtime.py`
- Create: `tests/test_factory_restart_recovery.py`
- Modify later in this task only after RED: `cptr/app.py`

**Interfaces:**
- `FactoryRuntime.recover_active_runs() -> list[str]` claims persisted non-terminal runs and schedules exactly one server-owned continuation per claimed run.
- `FactoryRuntime.reconcile_run(run_id) -> FactoryRunRecord` moves uncertain resumptions through `RECOVERING` rather than inventing success.
- startup integration stores `app.state.factory_runtime` and calls recovery after DB initialization.

- [ ] Write a subprocess restart test modeled on `tests/test_restart_recovery.py`: persist a non-terminal run, start CPTR, terminate, restart, and prove no duplicate transition/action event is generated.
- [ ] Add a RED test proving disappearance of an in-memory execution handle cannot yield `COMPLETE`.
- [ ] Implement recovery scheduling using run leases and persisted next action.
- [ ] Wire startup/shutdown lifecycle only after tests define the behavior.
- [ ] Re-run restart tests plus existing monitor restart recovery.

---

## Phase 2 — Victory and verification engine

### Task 5: Declarative gate plan and revision-bound evidence

**Files:**
- Create: `cptr/services/factory_gates.py`
- Create: `tests/test_factory_gates.py`
- Reuse/extend later: `cptr/services/verification.py`

**Interfaces:**
- `FactoryGateSpec(gate_id, category, required, applicability, invalidated_by_mutation)`.
- `FactoryGatePlan` contains immutable gate specs plus acceptance-criterion coverage.
- `FactoryGateStatus`: `PENDING`, `PASS`, `FAIL`, `NOT_APPLICABLE`.
- `resolve_gate_plan(repository_profile, configured_gates) -> FactoryGatePlan` preserves explicit required gates.
- `validate_gate_evidence(spec, result, evidence, *, current_revision, current_fingerprint) -> list[str]` returns deterministic failure reasons.

- [ ] RED-test every required category expected by the spec, explicit non-applicability reasons, missing gate detection, evidence-less PASS rejection, and stale revision/fingerprint rejection.
- [ ] Implement the minimal gate plan/evidence validator.
- [ ] Extend existing verification categories compatibly rather than removing legacy categories.
- [ ] Re-run existing `tests/test_verification.py` to prove backward compatibility.

### Task 6: Deterministic Victory judge and adversarial false-positive tests

**Files:**
- Create: `cptr/services/factory_victory.py`
- Create: `tests/test_factory_victory.py`

**Interfaces:**
- `FactoryVictoryDecision(passed, failures, satisfied_gate_ids, evaluated_revision, evaluated_fingerprint)`.
- `FactoryVictoryJudge.evaluate(*, gate_plan, gate_results, evidence, current_revision, current_fingerprint, unresolved_security_findings=()) -> FactoryVictoryDecision`.
- Judge has no model/provider dependency.

- [ ] Add RED tests proving false success prose cannot override a failed command gate.
- [ ] Add RED tests for missing required gate, required `PENDING`, required `FAIL`, non-applicable without reason, stale evidence, evidence digest mismatch, unresolved blocking security/adversarial finding, and incomplete acceptance coverage.
- [ ] Add a RED test proving all valid required current-revision evidence produces PASS.
- [ ] Implement the deterministic judge and keep its input/output serializable.
- [ ] Re-run all factory domain/gate/victory tests and existing independent verifier tests.

### Task 7: Machine-only success transition integration

**Files:**
- Modify: `cptr/services/factory_store.py`
- Extend: `tests/test_factory_victory.py`
- Extend: `tests/test_factory_persistence.py`

**Interfaces:**
- `SqlFactoryStore.authorize_victory(run_id, cycle_id, decision, idempotency_key) -> FactoryRunRecord` accepts only a passing `FactoryVictoryDecision` bound to the current revision/fingerprint and transitions `VICTORY_JUDGING -> COMMITTING` as `SYSTEM`.
- No generic transition call from a worker/model can simulate `machine_victory=True`.

- [ ] RED-test attempts to construct/submit a fake passing dict or model-produced decision and prove it is rejected.
- [ ] RED-test stale but previously passing decisions after a mutation fingerprint changes.
- [ ] Implement an opaque/validated decision provenance boundary owned by the Victory service/store integration.
- [ ] Re-run the adversarial suite.

---

## Phase 3 — GPT-5.6 Sol reasoning orchestration

### Task 8: Provider-neutral role runner

**Files:**
- Create: `cptr/services/factory_reasoning.py`
- Create: `cptr/models/factory_reasoning.py` only if separate tables materially improve cohesion; otherwise extend `cptr/models/factory.py` in this phase with a migration `0020`.
- Create: `tests/test_factory_reasoning.py`

**Interfaces:**
- `ReasoningRole` enum for Architect, Research, Skill Judge, Implementer, Debugger, Adversarial, Security, Verifier, Victory Judge.
- `ReasoningRequest` includes run/cycle, role, immutable mission/criteria references, bounded evidence IDs, schema ID, model policy, and budget.
- `FactoryReasoner.run(request) -> StructuredReasoningResult` validates schema and records provider/response identity, tokens/runtime/cost where available.
- Provider-specific Responses implementation remains behind an interface and stores separate continuation state per role.

- [x] RED-test role isolation, invalid JSON/schema failure, bounded retries, budget exhaustion, and no hidden-reasoning persistence.
- [x] Implement provider-neutral interface by reusing patterns in `OpenAISupervisorDirector`.
- [x] Add adaptive role policy so trivial deterministic phases do not invoke every role.
- [x] Verify security/adversarial/final high-risk roles select the strongest configured model policy without hardcoding a workstation model ID.

---

## Phase 4 — Skill Intelligence

### Task 9: Capability manifests and local inventory

**Files:**
- Create: `cptr/models/factory_capabilities.py`
- Create: `cptr/migrations/versions/0021_factory_capabilities.py`
- Create: `cptr/services/factory_capabilities.py`
- Create: `tests/test_factory_capabilities.py`

**Interfaces:**
- `CapabilityManifest` normalized fields exactly as specified in `docs/architecture/dark-factory.md`.
- `CapabilityInventory.discover_local(workspace) -> list[CapabilityManifest]` adapts repository/global Skills, CPTR built-ins, configured MCP servers, FDX/LSP/browser/Git/command providers.
- `CapabilityRequirement` normalizes task capability needs.

- [x] RED-test deterministic IDs, version/digest identity, permission/network representation, skill progressive disclosure, and duplicate normalization.
- [x] Implement adapters over existing Skills/MCP/tool services; do not duplicate their execution code.

### Task 10: Historical performance and trust-aware ranking

**Files:**
- Extend: `cptr/models/factory_capabilities.py`
- Extend: `cptr/migrations/versions/0021_factory_capabilities.py`
- Create: `cptr/services/factory_capability_ranking.py`
- Create: `tests/test_factory_capability_ranking.py`

**Interfaces:**
- `record_capability_outcome(...)` updates attempts, verified success/failure/regression, repair iterations, tokens/runtime/cost and confidence.
- `rank_capabilities(requirements, candidates, history, policy) -> list[RankedCapability]` first removes trust-ineligible candidates, then returns decomposed scores for fit/quality/history/maintenance/freshness/latency/token/cost.

- [x] RED-test that a high historical score never overrides `REJECTED`, `QUARANTINED`, revoked, stale-review-required or excessive-permission status.
- [x] RED-test deterministic ranking and low-sample confidence behavior.
- [x] Implement objective outcome updates only from verified factory outcomes.

---

## Phase 5 — External discovery and quarantine

### Task 11: Discovery providers with non-executable fetch

**Files:**
- Create: `cptr/services/factory_discovery.py`
- Create provider modules under `cptr/services/factory_discovery_providers/`
- Create: `tests/test_factory_discovery.py`

**Interfaces:**
- Providers for official docs/search, GitHub, package ecosystems, and MCP registries return metadata/source artifacts only.
- `DiscoveryBudget` enforces max providers/results/bytes/runtime.
- Fetched content lands in a quarantine cache and is never directly invoked.

- [x] RED-test research trigger policy, result/byte bounds, timeout behavior, and that fetched executable-looking content is not executed.
- [x] Implement providers using existing browser/web/MCP/network infrastructure and explicit configuration.

### Task 12: Trust/quarantine evaluator

**Files:**
- Create: `cptr/services/factory_trust.py`
- Create: `tests/test_factory_trust.py`

**Interfaces:**
- `TrustEvaluation` contains pin, digest, provenance, permissions, static/dependency/injection findings, capability-test result, final trust state.
- `FactoryTrustEvaluator.evaluate(candidate, policy) -> TrustEvaluation` fails closed.

- [x] RED-test mutable/unpinned source rejection, digest changes, manifest permission escalation, prompt-injection text, unsafe dependency/install instructions, and cache revalidation.
- [x] Implement static analysis and permission comparison without executing candidate code.
- [x] Add a constrained capability-test adapter only for candidates that reach quarantine-test eligibility.

---

## Phase 6 — Worker execution

### Task 13: Factory worktree and ownership controller

**Files:**
- Create: `cptr/services/factory_workers.py`
- Create: `tests/test_factory_workers.py`

**Interfaces:**
- `FactoryWorkerController.create_mutation_worker(run, cycle, repo_path) -> worker_id` delegates to `DirectCodingWorkerService`.
- `assign_read_only(...)` permits bounded parallel investigation.
- `assign_mutation(...)` enforces one writer per overlapping scope.
- `reconcile(...)` maps persisted worker IDs to current worktree/process state after restart.

- [x] RED-test dirty base protection, cross-workspace references, overlapping mutation ownership, restart reconciliation, quiescent cancellation, and cleanup.
- [x] Implement by composing Direct Coding Worker and command services, not shelling out to unmanaged worktrees.

### Task 14: Capability execution router

**Files:**
- Create: `cptr/services/factory_execution_router.py`
- Create: `tests/test_factory_execution_router.py`

**Interfaces:**
- Route approved manifests to CPTR file/coding, FDX, LSP, command/PTY, browser/web, MCP, SSH-configured, or optional terminal-plugin providers.
- Route rejects trust-ineligible or over-permissioned capabilities before provider invocation.

- [x] RED-test every trust/permission rejection before provider call.
- [x] Implement provider adapters with bounded outputs and evidence normalization.

---

## Phase 7 — Full Dark Factory loop

### Task 15: Orchestrator and phase handlers

**Files:**
- Create: `cptr/services/factory_orchestrator.py`
- Create phase handlers under `cptr/services/factory_phases/`
- Create: `tests/test_factory_orchestrator.py`
- Create: `tests/test_factory_failure_loop.py`

**Interfaces:**
- `FactoryOrchestrator.run_once(run_id) -> FactoryRunRecord` executes at most one resumable state action under a run lease.
- Each state handler consumes persisted inputs/evidence and produces a bounded `PhaseOutcome` with requested transition and durable artifacts.

- [x] RED-test the complete state progression with fake deterministic providers.
- [x] RED-test gate failure -> persisted evidence -> normalized failure -> Debugger/repair -> targeted verify -> full verify.
- [x] RED-test repeated failure triggering capability rediscovery rather than blind reruns.
- [x] Implement phase handlers incrementally with no monolithic prompt containing the whole factory loop.

### Task 16: Git/commit/push/CI lifecycle

**Files:**
- Create: `cptr/services/factory_git.py`
- Create: `cptr/services/factory_ci.py`
- Create: `tests/test_factory_git_ci.py`

**Interfaces:**
- Commit intent is bound to run/cycle/current revision and actual reviewed diff.
- Push uses existing Git/network policy + approval.
- CI state persists provider/check/run identity and exits early on failure.

- [x] RED-test duplicate commit recovery, stale verified revision, push approval, CI failure diagnosis-before-rerun policy, and changed revision invalidating previous CI PASS.
- [x] Implement bounded provider polling/event handling without sleep-based correctness assumptions.

**Phase 7 verification (2026-09-03):** 22/22 focused Phase 7 tests, 153/153 Dark Factory/recovery regression tests, and 431/431 full repository tests passed. Migration `0022 -> 0023 -> 0022 -> 0023`, Ruff, `git diff --check`, and changed-line credential scanning also passed. Repair budgets fail closed to `BLOCKED`, crash replay is idempotent, multi-cycle advancement is atomic, and CI uses one durable provider observation per orchestrator action with no sleep-loop correctness dependency.

---

## Phase 8 — ChatGPT MCP surface

### Task 17: Backend compact factory API

**Files:**
- Create: `cptr/routers/factory.py`
- Modify: `cptr/routers/__init__.py`
- Modify: `cptr/app.py`
- Create: `tests/test_factory_api.py`

**Interfaces:**
- start/status/events/evidence/message/pause/resume/approve/stop routes exactly as documented in the architecture spec.
- Owner/scope validation uses existing control authentication.
- Pagination and response size are bounded.

- [ ] RED-test ownership, idempotent start, transition authority, pagination, approval replay, pause/resume and cancellation quiescence.
- [ ] Implement routes as thin calls into factory services.

### Task 18: ChatGPT plugin adapter

**Repository:** `heidi-dang/chatgpt-computer-plugin`, using a new clean isolated worktree based on latest remote main. Preserve the currently dirty local checkout and never overwrite its modified files.

**Files:**
- Modify: `server/types.ts`
- Modify: `server/schemas/tools.ts`
- Modify: `server/client/computer-client.ts`
- Modify: `server/mcp.ts`
- Modify: `tests/mcp.test.ts`
- Add focused factory contract tests if needed.

**Interfaces:**
- `cptr_factory_start`
- `cptr_factory_status`
- `cptr_factory_events`
- `cptr_factory_evidence`
- `cptr_factory_message`
- `cptr_factory_pause`
- `cptr_factory_resume`
- `cptr_factory_approve`
- `cptr_factory_stop`

- [ ] RED-test exact compact tool set, bounded schemas, annotations, forwarding, owner-safe errors, and absence of internal state mutation primitives.
- [ ] Implement thin client forwarding only; no factory transition/trust/Victory logic in TypeScript.
- [ ] Run plugin typecheck/test/build and deployed-contract checks.

---

## Phase 9 — Benchmarking and learning

### Task 19: Factory metrics and capability learning

**Files:**
- Create/extend: `cptr/services/factory_metrics.py`
- Reuse: `cptr/services/mcp_usage_store.py`, `cptr/services/coding_benchmark.py`, `cptr/services/runtime_metrics.py`
- Create: `tests/test_factory_metrics.py`

**Interfaces:**
- Persist per-run/cycle/role/capability runtime, token, cost, attempts, repair iterations, gate latency and verified outcome.
- Real-work metrics remain observational; standardized benchmark scores remain separately comparable.

- [ ] RED-test that failed/blocked runs do not count as verified capability successes.
- [ ] RED-test no prompt/source/hidden reasoning is required in metrics.
- [ ] Implement longitudinal score inputs and regression summaries.

---

## Phase 10 — Production hardening

### Task 20: Failure injection, concurrency, security and live campaign

**Files:**
- Add focused tests under `tests/` for restart/concurrency/security/live behavior.
- Extend deployment/runbooks in `docs/architecture/dark-factory.md` and `docs/control-plane.md` after evidence exists.

- [ ] Restart CPTR at each critical boundary: after run create, worker create, mutation, verification PASS, Victory PASS, commit intent, commit, push, and CI observation.
- [ ] Race two recovery owners and prove one lease winner/no duplicate worker/action.
- [ ] Inject malicious skill/doc/MCP descriptions and prove policy/trust cannot be changed.
- [ ] Run explicit Victory false-positive campaign with failed required gates.
- [ ] Run bounded concurrency/soak and measure RSS, FDs, DB busy/error count, event-loop lag, queue pressure and leaked execution handles.
- [ ] Run full Python suite, backend lint/format check, frontend tests/build, plugin tests/typecheck/build, migration-from-fresh and migration-from-current database.
- [ ] Inspect final diffs and `git diff --check`; scan for secrets, host-specific values, debug artifacts and unrelated churn.
- [ ] Push verified coherent commits to the same implementation branch/PR; monitor CI and repair failures before rerun.
- [ ] Deploy only under the repository's production policy/approval, then verify health plus a real factory start/status/evidence/restart flow from ChatGPT.

## Completion evidence

The implementation is complete only when the final report can cite concrete evidence for:

1. architecture and state machine;
2. persistence/migrations and restart recovery;
3. machine-only Victory authority and adversarial false-positive prevention;
4. Sol role isolation and structured outputs;
5. dynamic local + external capability intelligence;
6. external trust/quarantine and permission enforcement;
7. worker/worktree execution and cleanup;
8. compact MCP surface;
9. benchmarks/metrics and skill effectiveness learning;
10. concurrency/security/live production gates;
11. exact commits, branch/PR and CI conclusions;
12. remaining limitations without inflated scores.
