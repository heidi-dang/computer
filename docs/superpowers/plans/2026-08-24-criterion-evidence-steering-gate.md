# Criterion Evidence and Steering Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent autonomous monitors from reaching VERIFIED/COMPLETE without mandatory steering evidence, make queued steering deliverable during a long-running owned tool call, and enforce narrow assignment file scope without breaking normal repository investigation.

**Architecture:** Add a deterministic acceptance-evidence gate in `AutonomousSupervisor` after generic independent verification and before any scope transition. The gate derives its contract from the immutable scope/assignment metadata and requires authoritative control-message records plus same-worker post-consumption workspace evidence. Extend the existing owned execution boundary with a cooperative control checkpoint/interrupt path so a queued autonomous control can stop or yield a long-running owned tool execution and resume the same task context. Carry an explicit inspection-scope policy into worker tool dispatch and reject out-of-scope file operations before filesystem access.

**Tech Stack:** Python 3.14, asyncio, FastAPI/SQLAlchemy, unittest, existing CPTR task/control-message stores, existing native/tool execution ownership.

**Spec:** `/home/shacker/.codex/attachments/cd04cfe3-193e-4742-ae1f-5782fd56c1f5/pasted-text.txt`

## Global Constraints

- Do not push, merge, deploy, modify DNS, rotate credentials, or change the MCP/plugin repository.
- Use only disposable acceptance workspaces and synthetic test data.
- Preserve writer-lease release/recovery, cancellation authority, approval polarity, redaction, Git diff, and non-Git diagnostics.
- Never use worker prose as proof of steering, mutation, or scope compliance.
- Do not hardcode paths, fixture names, users, ports, models, tokens, or credentials.
- Generic checks such as `durable_terminal_success` and `git_diff_check` are necessary infrastructure evidence but cannot substitute for criterion-specific evidence.

---

### Task 1: Establish the focused repair branch and trace current contracts

**Files:**
- Modify: none initially
- Test: existing `tests/test_steering_provenance_and_redaction.py`, `tests/test_supervisor_core.py`, `tests/test_execution_cancellation.py`, and tool-execution tests discovered during tracing

**Interfaces:**
- Consumes: current `ec35eed184fe9b4d71f8c81ff38196ed762b670a` checkout
- Produces: branch `fix/criterion-evidence-steering-gate`, a written root-cause map, and exact test insertion points

- [ ] **Step 1: Create the focused branch from the current accepted repair checkout**

Run:

```bash
git switch -c fix/criterion-evidence-steering-gate
```

- [ ] **Step 2: Trace supervisor verification and final-gate data flow**

Inspect `cptr/services/supervisor.py`, `cptr/services/verification.py`, `cptr/services/supervisor_director.py`, `cptr/services/control_store.py`, and the existing steering tests. Record where `ScopeStatus.VERIFIED`, `Decision.scope_satisfied`, and `Decision.goal_satisfied` are assigned, including the exact evidence payload passed to the director.

- [ ] **Step 3: Trace control delivery and owned tool execution**

Inspect `cptr/services/agent_service.py`, `cptr/utils/chat_task.py`, the native/tool execution ownership helpers, and cancellation tests. Identify the boundary where a control transitions from QUEUED to CONSUMED and whether a long-running tool invocation has a cooperative checkpoint or cancellation handle.

- [ ] **Step 4: Trace worker tool scope propagation**

Inspect task prompt construction, tool registration/dispatch, and the implementations of `read_file`, `write_file`, `edit_file`, `search_files`, and `list_directory`. Identify the narrowest shared authorization point before filesystem access and the existing representation of the selected workspace root.

- [ ] **Step 5: Record the single hypotheses before implementation**

The hypotheses to test are:

1. The false positive occurs because supervisor code accepts generic verification/director success without a deterministic mandatory-evidence predicate.
2. Long-running steering remains QUEUED because control consumption is only checked after the active model/tool turn returns and no owned execution checkpoint interrupts or yields to the same task.
3. `workspace_scope=current` is prompt-only because tool dispatch does not validate requested paths against assignment-derived allowed paths.

---

### Task 2: Add RED tests for mandatory steering evidence

**Files:**
- Modify: `tests/test_steering_provenance_and_redaction.py`
- Modify: `tests/test_supervisor_core.py`
- Modify: `tests/test_autonomous_e2e.py` if the end-to-end harness is the smallest reproduction location

**Interfaces:**
- Consumes: existing `ScopeRecord`, `SteeringRequest`, `ControlMessage`, `Decision`, and supervisor test doubles
- Produces: failing tests that prove generic verification cannot satisfy a steering-required criterion

- [ ] **Step 1: Write a test for no steering control**

Create a scope whose acceptance criterion explicitly requires same-worker steering and a worker result containing only `BASELINE`. Use a director that returns `scope_satisfied=True` and generic verification evidence with `durable_terminal_success=True` and `git_diff_check=True`. Assert the scope is not VERIFIED, the monitor is not COMPLETE, and a `steering_provenance` record reports missing evidence.

- [ ] **Step 2: Write a test proving QUEUED is insufficient**

Persist a steering request with `control_message_id` and status `QUEUED`, but no `consumed_task_id`, `consumed_message_id`, `consumed_at`, or post-consumption fingerprint. Assert the scope cannot become VERIFIED.

- [ ] **Step 3: Write a test for wrong-worker consumption**

Persist a request with `intended_task_id=task_1` and a consumed message attributed to `task_replacement`. Provide the expected file mutation from the replacement task. Assert same-worker steering verification remains false.

- [ ] **Step 4: Write a test for consumed-without-effect**

Persist a fully consumed same-worker control with baseline and post snapshots that are identical. Assert `effect_status` is not `EFFECT_OBSERVED` and the scope remains non-success.

- [ ] **Step 5: Run only these tests and confirm RED**

Run:

```bash
python -m unittest tests.test_steering_provenance_and_redaction tests.test_supervisor_core -v
```

Expected: the new assertions fail because no deterministic mandatory-evidence gate exists for the reproduced false-positive path.

---

### Task 3: Implement the deterministic criterion-evidence gate

**Files:**
- Modify: `cptr/services/supervisor.py`
- Modify: `cptr/services/supervisor_director.py` only if the director payload needs an explicit non-authoritative evidence summary
- Modify: `cptr/services/verification.py` only if infrastructure and criterion result need separate structured fields

**Interfaces:**
- Consumes: immutable scope acceptance criteria, steering request records, control-store message records, baseline/post workspace fingerprints, and generic independent verification
- Produces: a deterministic predicate such as `_required_criterion_evidence(scope) -> dict[str, Any]` that must pass before `ScopeStatus.VERIFIED`

- [ ] **Step 1: Add a failing assertion for the gate call site**

Ensure the test harness can observe a structured `criterion_evidence` or equivalent evidence record with `status` and `missing` fields. The record must distinguish infrastructure checks from criterion satisfaction.

- [ ] **Step 2: Implement criterion classification without hardcoded fixture names**

Detect steering-specific requirements from normalized acceptance-criterion language and/or explicit structured scope metadata. Recognize concepts such as steering/control consumption, same worker, post-consumption mutation, and `EFFECT_OBSERVED`; do not recognize fixture filenames as special cases.

- [ ] **Step 3: Implement the mandatory evidence predicate**

For a same-worker steering criterion require all of:

```python
control_message_id
status == "CONSUMED"
intended_task_id
consumed_task_id
consumed_task_id == intended_task_id
consumed_message_id
consumed_at
baseline_fingerprint
post_consumption_fingerprint
qualifying_change_after_consumption
effect_status == "EFFECT_OBSERVED"
```

Return a bounded failure reason for every missing or contradictory item.

- [ ] **Step 4: Enforce the predicate before VERIFIED**

After generic independent verification and before applying `scope.transition(ScopeStatus.VERIFIED)`, persist criterion evidence. If it fails, keep the scope non-verified and route through the existing deterministic repair/block path. The director may explain or plan next work but may not override the predicate.

- [ ] **Step 5: Enforce the same predicate at the final gate**

Before accepting `goal_satisfied`, re-evaluate every scope’s mandatory criterion evidence. A director decision of `goal_satisfied=True` must be rejected when any required evidence is absent or contradictory.

- [ ] **Step 6: Run the new focused tests and confirm GREEN**

Run the tests from Task 2. Expected: all new false-positive, queued-only, wrong-worker, no-effect, and generic-override tests pass.

---

### Task 4: Add RED tests and implement active steering delivery during long-running tools

**Files:**
- Modify: `tests/test_execution_cancellation.py` or the existing task/steering test module selected during tracing
- Modify: `cptr/services/agent_service.py`
- Modify: `cptr/utils/chat_task.py`
- Modify: the existing owned native/tool execution module identified in Task 1
- Modify: `cptr/services/control_store.py` only if an atomic delivery/checkpoint transition is required

**Interfaces:**
- Consumes: owned task execution identity, control-message queue, cancellation/quiescence handle, and current worker context
- Produces: a same-task cooperative checkpoint/interrupt path that changes QUEUED to CONSUMED for the intended task and resumes it without replacement-worker substitution

- [ ] **Step 1: Write the long-running-tool RED test**

Start one real task in a disposable fixture with an owned harmless long-running local command. Enqueue exactly one autonomous control while the command is active. Assert before the fix that the control remains QUEUED beyond a bounded checkpoint window and no same-task post-control mutation occurs.

- [ ] **Step 2: Write the same-worker resume assertions**

Extend the test to require `intended_task_id == consumed_task_id`, exactly one `consumed_message_id`, exactly one `EFFECT_OBSERVED` mutation, and no replacement worker task.

- [ ] **Step 3: Implement the smallest cooperative checkpoint**

At the owned execution boundary, periodically or at tool-return-safe checkpoints, inspect pending controls for the owning task. If a control exists, safely interrupt/yield the owned local tool execution through the existing task-owned cancellation mechanism, atomically claim the control for the same task, and resume the same worker context with the control content. Do not globally kill processes and do not create a replacement worker.

- [ ] **Step 4: Preserve cancellation precedence**

If task or monitor cancellation wins while a control is queued or being delivered, cancellation must invalidate the control and terminate owned execution. No control may be consumed after cancellation.

- [ ] **Step 5: Run the focused steering, cancellation, and exact-once tests**

Run the targeted test module(s) and verify the long-running-tool test is GREEN with same-task consumption and one mutation.

---

### Task 5: Add RED tests and implement enforceable narrow assignment scope

**Files:**
- Modify: `tests/test_scope_enforcement.py` (create if no focused module exists)
- Modify: the shared worker tool dispatch/authorization module identified in Task 1
- Modify: `cptr/utils/chat_task.py` or assignment construction only to propagate explicit scope metadata

**Interfaces:**
- Consumes: workspace root, assignment scope mode, explicitly named files, and files created by the current task/monitor
- Produces: a tool-level path-policy check returning a bounded scope-violation result before filesystem access

- [ ] **Step 1: Write the narrow-scope RED test**

Create a disposable workspace containing `fresh-target.txt` and `historical-target.txt`. Give a worker assignment with `inspection_scope=assignment` naming only `fresh-target.txt`. Assert a read of `fresh-target.txt` succeeds and a read of `historical-target.txt` is denied with a controlled scope violation.

- [ ] **Step 2: Write traversal and symlink tests**

Assert `../historical-target.txt`, absolute paths, and symlinks resolving outside the workspace are denied under assignment scope.

- [ ] **Step 3: Write repository-investigation-mode compatibility test**

Use `inspection_scope=workspace` for a normal engineering task and assert repository listing, search, and source-file reads remain allowed within the selected workspace.

- [ ] **Step 4: Implement shared tool-level path enforcement**

Resolve every requested path against the workspace root, reject traversal/outside resolution, and when `inspection_scope=assignment` allow only explicitly named paths, required parent directories, and files recorded as created by the current task/monitor. Return a bounded non-secret error such as `scope violation: file is outside assignment scope`.

- [ ] **Step 5: Propagate scope metadata through assignment and continuation**

Ensure steering continuations inherit the original assignment scope and cannot widen it implicitly. Normal repository tasks retain workspace scope.

- [ ] **Step 6: Run narrow-scope and workspace-mode tests and confirm GREEN**

Run the new focused test module and verify both restrictive and normal workflows pass.

---

### Task 6: Full regression and local readiness gate

**Files:**
- Modify: only files required by failing tests and focused implementation

**Interfaces:**
- Consumes: all repaired contracts
- Produces: local-only repair evidence; no push/deploy/merge

- [ ] **Step 1: Run all focused evidence, delivery, scope, lease, approval, cancellation, redaction, Git, and non-Git tests**

- [ ] **Step 2: Run the complete current CPTR test suite**

```bash
python -m unittest discover -s tests -v
```

- [ ] **Step 3: Run targeted Ruff and format checks for every modified Python file**

- [ ] **Step 4: Run compile/import/start smoke and `git diff --check`**

- [ ] **Step 5: Review the complete diff for secrets, fixture artifacts, hardcoded paths/models/tokens, and unrelated changes**

- [ ] **Step 6: Verify the branch remains local and the plugin repository is untouched**

- [ ] **Step 7: Calculate readiness only from fresh evidence**

The repair is ready for a new real ChatGPT-native 15-tool acceptance test only if:

- steering-required criteria cannot become VERIFIED without mandatory evidence;
- QUEUED cannot count as CONSUMED;
- same-worker consumption and EFFECT_OBSERVED are proven;
- long-running-tool steering has deterministic bounded delivery/resume behavior;
- narrow assignment scope is enforced while workspace investigation remains functional;
- lease, approval, cancellation, redaction, Git, and non-Git regressions pass;
- full CPTR suite passes;
- readiness is at least 9.5/10;
- no P1 defect remains.
