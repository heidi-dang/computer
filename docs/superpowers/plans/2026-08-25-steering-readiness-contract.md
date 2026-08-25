# CPTR Steering Readiness and Diff Contract Implementation Plan

> **For agentic workers:** Use the parallel implementation assignments from the parent task; each assignment has a disjoint write set and must leave focused tests behind.

**Goal:** Make steering eligible only after setup readiness, bind autonomous steering to the intended worker, expose durable control evidence, include untracked files in diffs, and minimize external path metadata without weakening cancellation or scope safety.

**Architecture:** Keep the existing CPTR task/control architecture. Add readiness as durable steering provenance and gate consumption/verification on it; keep replacement workers ineligible for same-worker steering criteria. Extend existing task read responses with bounded control records, repair Git diff enumeration for untracked files, and redact workspace roots at the external boundary.

**Tech Stack:** Python, FastAPI, SQLAlchemy/SQLite, asyncio, unittest; existing TypeScript MCP adapter and npm contract tests.

## Global Constraints

- Preserve task and autonomous authoritative cancellation.
- Preserve assignment scope denial and writer-lease behavior.
- Do not expose secrets, cookies, tokens, or unnecessary absolute host paths.
- Use disposable fixtures only for live acceptance.
- Do not modify plugin source unless the deployed schema audit proves a compatibility gap.

### Task 1: Steering readiness and same-worker terminal binding

**Files:**
- Modify: `cptr/services/agent_service.py`, `cptr/routers/control.py`, `cptr/services/supervisor.py`, `cptr/services/control_store.py`
- Test: existing steering/provenance/control delivery tests plus new focused regression tests

Add a durable readiness state to steering provenance. A steering request remains pending until the intended worker has persisted setup readiness. Same-worker steering criteria must fail closed if the intended worker terminates before readiness/consumption, and repair delegation must not replace that criterion with another worker.

### Task 2: Diff and task-control observability

**Files:**
- Modify: `cptr/utils/git.py`, `cptr/services/agent_service.py`
- Test: Git diff and AgentService control-output tests

Include bounded untracked-file entries in workspace diffs and return bounded durable control delivery records from task and task-output reads.

### Task 3: External metadata minimization and cancellation contract tests

**Files:**
- Modify: `cptr/services/agent_service.py`, external response shaping only if required
- Test: redaction/path-minimization tests and cancellation/control-race tests

Remove unnecessary absolute workspace roots from externally returned evidence while preserving internal execution. Document and test that queued controls are invalidated when CPTR receives cancellation; do not bypass ChatGPT destructive confirmation.

### Task 4: Integration verification

Run focused tests, the full CPTR suite, changed-file lint/format checks, plugin tests/typecheck/build/audit, deployed bundle schema inspection, and a fresh clean-fixture 15-tool acceptance. Record any user-only or connector-cache blocker rather than claiming success.
