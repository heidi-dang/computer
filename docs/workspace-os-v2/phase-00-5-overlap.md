# Workspace OS v2 — Phase 0.5 Overlap Coordination

## Purpose

Prevent Workspace OS work from overwriting or depending blindly on stale Direct Coding workers and parallel Capability OS/Memory work.

## Current evidence

The legacy `computer` workspace exposes seven Direct Coding workers marked `WORKING`. At inspection time all seven had empty `active_command_ids`, no `integrated_at`, and no `closed_at`. Several have zero changed files; one known worker has four changed files. A `WORKING` label alone is therefore not proof that an overlapping implementation is actively executing.

The external memory-control read path is still rejected with `CONTROL_SCOPE_REQUIRED: memory:read`. The Memory Core implementation itself is present and internally testable; public/control integration must respect the final scope contract.

Capability OS compatibility work exists around the live six-operation contract `inspect/resolve/forge/execute/acquire/reflect`. Workspace OS must consume, not rename or repurpose, those operations.

## Required overlap classification

Before changing a surface touched by an existing worker, classify that worker using:

- active command IDs and recent command IDs;
- actual worker diff/changed paths;
- branch divergence from its base/current main;
- associated pull request if one exists;
- latest meaningful activity;
- whether equivalent changes already landed elsewhere.

Allowed classifications:

- `active_overlap`
- `stale_noop`
- `stale_with_changes`
- `already_integrated_unclosed`
- `unrelated`

## Safety policy

- Never discard or close `stale_with_changes` automatically.
- Never block Workspace OS solely because a record says `WORKING`.
- Never mutate an overlapping checkout from another task.
- Use isolated CPTR worktrees for Workspace OS changes.
- Public memory/MCP integrations wait for stable access/contract interfaces; internal independent schema/service work may proceed.
- Merge and deployment remain outside this phase.

## Acceptance

Phase 0.5 is satisfied when overlapping work is treated from live diff/activity evidence, Workspace OS uses isolated worktrees, and compatibility-sensitive surfaces are not overwritten by assumption.
