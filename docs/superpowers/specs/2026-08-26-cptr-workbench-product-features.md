# CPTR Live Workbench and Task UX Design

## Goal

Make CPTR work feel like one organized engineering workspace: direct coding exposes the complete 24-tool contract, agent execution opens the same Live Workbench, task progress is understandable at a glance, and transient runtime artifacts stay grouped under one task-owned directory.

## Scope

This change covers four product improvements:

1. Finish the MCP Apps Live Workbench presentation and interaction flow.
2. Preserve and verify the complete 24-tool direct-coding and agent surface already present on `main`.
3. Keep transient task artifacts under the configured CPTR task-runtime root.
4. Improve execution UX with progress, current operation, delivery state, output, changes, evidence, and safe controls.

Authentication, OAuth, CPTR authorization, approval policy, worker lifecycle, and AWS deployment are out of scope.

## Architecture

The TypeScript adapter remains a thin MCP boundary. It issues target-bound Live Workbench tickets and forwards the private CPTR token only to CPTR. The browser bundle consumes the replayable server-authoritative stream, reduces events into a bounded state model, and renders progress and evidence without polling.

The Python CPTR runtime remains the owner of execution and transient state. The existing `TASK_ROOT`/`task_runtime_dir()` layout is the canonical home for task-owned agent state, attachments, browser state, and command output. Existing durable task/chat data remains compatible; no historical workspace cleanup is performed.

## User-visible behavior

- `cptr_start_task`, `cptr_execute_task`, and `cptr_monitor_autonomous` open the Live Workbench resource.
- The header shows the target, durable status, connection state, active worker, and live sequence.
- A progress rail reflects lifecycle phases without inventing completion.
- Activity, Terminal, Tools, Changes, and Evidence remain separate views.
- The Workbench shows the most recent operation and control-delivery state when those events exist.
- Stop and Steer remain scoped to the target; steering uses one caller idempotency key per click.
- Replayed or out-of-order events do not duplicate visible activity.
- Terminal state disables controls and stops reconnect attempts.
- The UI never renders bearer tokens, host filesystem paths, raw prompts, or unbounded worker output.

## 24-tool contract

The adapter already exposes exactly 24 tools. This work adds regression coverage that asserts all nine direct-coding tools, the agent/task tools, autonomous tools, annotations, idempotency fields, and the Live Workbench resource remain present. No tool is removed or renamed.

## Runtime organization

The backend’s `CPTR_TASK_ROOT` defaults below CPTR’s data directory and can be configured. Each task runtime has `agent/`, `attachments/`, `browser/`, and `command-output/` categories. CPTR-spawned provider state, staged attachments, and command logs use this root. Existing external provider profiles and compatibility workspace state are not deleted or migrated by this feature.

## Testing

- TypeScript reducer tests cover lifecycle progress, active operation, control delivery, terminal quiescence, sequence deduplication, and bounded output.
- MCP contract tests cover all 24 tools and Workbench metadata on both task creation paths.
- Existing Python task-runtime and full CPTR tests remain the regression gate.
- The built browser bundle is checked with the repository’s build/typecheck/test commands and rendered through the local CPTR web endpoint.
