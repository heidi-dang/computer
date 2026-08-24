# CPTR Phase 3 Parallel Build Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or **superpowers:executing-plans** to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Heidi `/build` mutation work through a durable, bounded CPTR DAG whose mutation nodes execute in isolated Git worktrees, whose completions are fenced by authoritative evidence, and whose results are integrated into the canonical workspace only through bounded conflict-aware Git operations.

**Architecture:** Keep the existing authenticated CPTR gateway and native transcript as the only execution/rendering authority. Extend the existing durable `FlowDeckBuildNode` state with node role, branch, owner, fencing, evidence, and integration metadata. Add a small worktree lifecycle/integration service and a durable scheduler used by `run_build_agent` for real Git repositories. Each child dispatch is authenticated against the canonical workspace, then explicitly authorized as a worktree belonging to that repository. Existing sequential behavior remains the fallback for non-Git test fixtures.

**Tech Stack:** Python 3.13, FastAPI/Starlette, SQLAlchemy async + SQLite, Alembic, asyncio, Git worktrees, pytest/unittest, existing CPTR/FlowDeck gateway and event contracts.

**Spec:** `/home/shacker/.codex/attachments/948909d6-812e-4e55-968e-30824e9835eb/pasted-text.txt` (Phase 3 Gate B; provider/model work intentionally excluded by the current user request).

**Global Constraints**

- Do not call Replit Agent, provider discovery, or model-selection code.
- Do not push, deploy, publish, rotate credentials, reboot, reset, clean, or discard unrelated workspace changes.
- Canonical workspace mutations must be fenced and must not occur during child execution.
- Every child must retain parent chat/message/run identity and use `dispatch_authenticated_specialist`.
- Worktree paths and Git operations must be bounded to the authenticated repository; no shell or unrestricted tool authority is added.
- A late or stale child completion must not resurrect a cancelled or fenced node.
- Emit lifecycle state through existing FlowDeck events; do not add a second transcript renderer.

## Task 1: Extend durable node lifecycle metadata

- [x] Add persisted role/branch/owner/fencing/evidence/integration/retry fields to `FlowDeckBuildNode`.
- [x] Add Alembic migration `0009` preserving existing rows and defaults.
- [x] Extend `create_build_nodes`, claim, and finish APIs so node attempts carry ownership and authoritative terminal evidence, with stale/cancelled writers rejected.
- [x] Add unit and migration coverage for idempotent DAG creation, fencing, evidence, and late completion.

## Task 2: Add isolated worktree lifecycle and bounded Git integration

- [x] Add a CPTR worktree service that resolves the canonical repository, creates one branch-backed worktree per mutation node from one common base, records exact paths, and rejects traversal/symlink escapes.
- [x] Add read-only changed-path/overlap calculation and bounded commit/cherry-pick integration helpers; no push or deploy operations.
- [x] Clean up only task-owned worktrees/branches after terminal integration and expose orphan detection for restart recovery.
- [x] Add tests for common-base isolation, path fencing, non-overlap integration, overlap detection, conflict/manual-review state, and cleanup.

## Task 3: Authorize worktree execution through the existing CPTR gateway

- [x] Add an optional execution-workspace field to specialist dispatch while retaining the canonical authenticated workspace as the authority.
- [x] Validate that an execution path is an actual Git worktree of the authenticated repository before coding or testing.
- [x] Run native coding/test loops inside the isolated path, with leases and runtime fencing keyed to that path while preserving canonical ownership and parent identity.
- [x] Add adversarial tests for sibling repositories, symlink aliases, path traversal, direct shell/CodeAct escape, and unauthenticated worktree dispatch.

## Task 4: Wire a durable bounded parallel Build Agent

- [x] Build a deterministic bounded DAG for real Git `/build` runs with independent backend/frontend mutation branches and an explicit integration barrier.
- [x] Persist the DAG before dispatch, claim ready nodes transactionally, run only bounded dependency-ready batches, and pass parent chat/message/run identity into every child.
- [x] Record per-node CPTR operations/attempts/evidence and branch-local steering/retry events; repeated failure ends in durable diagnosis/manual review.
- [x] Integrate only successful authoritative branches, mark actual overlap/conflict outcomes durably, and run completion checks against the integrated canonical workspace.
- [x] Preserve the existing non-Git sequential fallback for isolated unit fixtures and existing compatibility tests.

## Task 5: Cancellation, recovery, transcript, and API acceptance

- [x] Make parent cancellation stop scheduling, cancel pending/running nodes, preserve unknown active attempts, and prevent late completion or resurrection.
- [x] Add restart/orphan recovery that detects task-owned worktrees and stale attempts, acquiring the existing recovery lease before reconciliation.
- [x] Verify native FlowDeck event/transcript payloads expose node start/finish/integration/conflict/manual-review state without a parallel renderer.
- [ ] Add authenticated `/build` and native end-to-end regression coverage for parallelism, persistence, cancellation, reconnect, cleanup, and no duplicate orchestration.

## Task 6: Verification and handoff

- [x] Run focused Phase 3 tests first, then backend/FlowDeck/CodeAct/frontend checks available in the workspace.
- [x] Run compile and changed-file lint checks, migration tests, and a fresh Git/status/worktree audit.
- [ ] Report exact verified scope, any environment-blocked checks, and leave the canonical branch unpushed and deployment-free.

