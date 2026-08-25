# FlowDeck Phase 3 Acceptance Record

Status: **qualified**  
Scope: Phase 3 only; Phase 4 and later work are excluded.

## Provider gate

- Executable provider: DeepSeek OpenAI-compatible connection.
- Exact model: `deepseek-v4-flash`.
- Positive verification: provider model enumeration and exact model verification both returned HTTP 200.
- Managed Replit OpenAI discovery: HTTP 405 remains `unverified`, non-executable, and was not used as a fallback.
- No credentials were rotated, copied, or written to the disposable workspace.

## Authenticated live smoke

The disposable harness used a temporary SQLite database, temporary Git repository, temporary authenticated user, and the existing CPTR native task path.

- Authenticated Computer chat listing: passed.
- Normal native Heidi chat request: HTTP 200; assistant message reached authoritative `done=true`.
- Durable message identity: passed; returned message ID matched the terminal durable record.
- Native transcript output: passed; terminal message contained native output items.
- Repeated cancellation: both cancellation requests returned HTTP 200.
- Cancellation terminalization: message was terminal before the cancel request returned and remained terminal after a delay.
- Late resurrection: no post-cancel state change observed.
- Native task cleanup: zero active native tasks after cleanup.
- Worktree cleanup: zero disposable worktree artifacts.
- Workspace residue: only the expected `.gitignore` metadata file created by native chat export.

The authenticated provider-backed `/build` smoke had already completed successfully against the same positively verified DeepSeek model, including parallel backend/frontend children, authoritative tester/build verification, steering checkpoints, integration, and cleanup. The latest cancellation changes were revalidated against the complete FlowDeck lifecycle suite.

## Automated acceptance matrix

- Backend aggregate: **218 passed**, 43 subtests.
- Focused Phase 3 lifecycle/transcript/realtime/gateway/coding/tester/worktree/migration suite: **95 passed**, 5 subtests.
- Native cancellation regression suite: **22 passed**.
- Python compilation: passed.
- Changed-file Ruff: passed.
- Frontend typecheck: passed.
- Frontend production build: passed.
- Git diff checks: passed.
- API workflow restart and startup: passed.

The focused suites cover fencing, stale-attempt reconciliation, recovery/reconnect, steering, queued and repeated cancellation, orphan cleanup, transcript identity/order/deduplication/terminal behavior, authority boundaries, workspace escape protection, same-workspace conflicts, and parallel branch ownership.

## Root causes repaired during this pass

1. A task cancelled before its first scheduling turn could leave its assistant message nonterminal because the native coroutine cleanup handler never ran.
2. The endpoint trusted task cancellation alone and did not durably finalize an unfinished assistant message when the task had already exited or never entered its coroutine.

The repair awaits cancellation cleanup and durably finalizes any remaining unfinished assistant message, rejecting pending function calls without creating a second execution path.

## Qualification decision

**9.5/10 — qualified for Phase 3.**

There are no open P0 or P1 defects. CPTR remains the sole model/tool execution authority; FlowDeck remains the lifecycle, fencing, evidence, steering, and completion authority. No fallback model, alternate transport, fake availability, MCP, FDX, or Phase 4 behavior was enabled.