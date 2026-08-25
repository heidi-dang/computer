# FlowDeck Phase 5 Audit Acceptance Record

Status: **qualified and frozen**
Scope: Phase 5 audit lifecycle, repository understanding, analysis, and results UX.

## Qualification decision

**9.5/10 — qualified for Phase 5.**

There are no open P0 or P1 defects. The audit path preserves UNKNOWN and
UNVERIFIED outcomes, never fabricates readiness, and keeps CPTR as the only
model/tool execution authority. Phase 6 and later work are excluded.

## Authenticated end-to-end audit smoke

The disposable harness used temporary SQLite persistence, a temporary
authenticated user, and two temporary repositories:

- Clean repository: authenticated `POST /v1/flowdeck/audits` reached
  `succeeded`.
- Intentionally flawed repository: authenticated `POST /v1/flowdeck/audits`
  reached `succeeded`; analysis remained `unverified` rather than claiming a
  false pass.
- Both runs emitted exactly one `AUDIT_REPOSITORY_FACTS_COLLECTED` and one
  `AUDIT_ANALYSIS_CREATED` event.
- Both runs emitted durable authenticated child evidence, terminal step
  evidence, and `RUN_COMPLETED`.
- Audit scope and completion-contract events preceded execution.
- The reserved-run path was exercised; retry-safe initialization does not
  duplicate facts or analysis.

The qualification harness used a deterministic fixture model callback so that
repository, lifecycle, identity, and evidence behavior could be tested without
accessing or rotating provider credentials. Provider behavior remains
fail-closed and is covered by the provider discovery and availability tests.

## Acceptance matrix

- Full backend regression: **230 passed**, 43 subtests.
- Focused audit, HTTP, coordinator, repository, and analysis suite:
  **35 passed**.
- Specialist, authenticated gateway, Build Agent, parallel build, CodeAct,
  coding, and tester suite: **79 passed**, 14 subtests.
- Migration, provider discovery, realtime smoke, restart recovery, and
  read-only/canonical Git suite: **21 passed**.
- Python compilation: passed.
- Ruff: passed.
- Frontend typecheck: passed with 0 errors.
- Frontend production build: passed.
- Existing visual regression suite: **12 passed**.
- Git diff checks: passed.
- API and web workflow restart/startup: passed.

The frontend visual runner logs showed connection-refused warnings when its
isolated web server attempted to proxy to an inactive local `9741` backend;
all 12 visual assertions still passed and the managed API workflow remained
healthy.

## Defect repaired during qualification

The HTTP audit route reserves the durable run before scheduling the
coordinator. The coordinator consequently observed `created=False` and
skipped repository facts and analysis because initialization was incorrectly
guarded by the run-created flag. The first end-to-end audit smoke reproduced
the missing events.

The repair makes initialization idempotent by checking for the durable
`AUDIT_ANALYSIS_CREATED` event instead. A regression test covers the reserved
run path, and the clean/flawed HTTP smoke confirms both events are present in
terminal status.

## Security and authority gates

- Authentication and workspace ownership remained enforced.
- Read-only audit inventory does not execute commands, invoke providers, follow
  escaping symlinks, or mutate the workspace.
- Specialist dispatch remains authenticated, depth-limited, and read-only.
- Durable attempts, fencing, cancellation, recovery, steering, reconnect,
  idempotency, transcript identity/order, duplicate suppression, and cleanup
  remain covered by the regression suite.
- CodeAct remains disabled by default and role/qualification controlled.
- HTTP 405 provider discovery remains `unverified`, non-executable, and never
  triggers fallback or model switching.
- No MCP, FDX, deployment, publishing, DNS, reboot, credential rotation, or
  security weakening was performed.