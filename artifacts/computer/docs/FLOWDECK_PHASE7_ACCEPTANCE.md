# FlowDeck Phase 7 Managed Runtime Acceptance

Status: **qualified and frozen**
Score: **9.1/10**

Phase 7 adds an authenticated, workspace-owned managed preview runtime without
changing CPTR’s native execution authority or the frozen Phase 1–6 contracts.

## Covered behavior

- Discovers only supported project commands from known manifests and entrypoint
  files; arbitrary shell strings are never evaluated.
- Allocates only approved preview ports and injects the managed `PORT`.
- Runs in the canonical owned workspace with a bounded process group and
  bounded in-memory logs.
- Reports `starting`, `running`, `crashed`, `unknown`, and `stopped` truthfully.
- Confirms both port availability and an HTTP response before claiming healthy.
- Persists lifecycle events and authoritative runtime/verifier evidence through
  the existing FlowDeck run state.
- Handles stop/cancellation through the existing durable cancellation state
  machine; monitor cancellation is joined before the stop event is written.
- Reports UNKNOWN after process loss/reconnect rather than fabricating a
  healthy preview.
- Provides authenticated status, stop, and preview proxy endpoints.
- Adds responsive managed-preview UX with status, logs, crash/UNKNOWN notices,
  iframe preview, and desktop/narrow layout support.

## Verification

- Full backend regression: **236 passed**, 43 subtests.
- Focused runtime and FlowDeck HTTP tests: passed.
- Ruff, Python compilation, and diff checks: passed.
- Frontend svelte-check: **0 errors**.
- Frontend production build: passed.
- Visual regression: **16 passed** at desktop and narrow viewports.
- API and web workflows restarted successfully and remain running.
- A cold desktop fixture timeout was reproduced, traced to first-load
  compilation readiness, and repaired with a bounded readiness wait; the full
  visual suite then passed.

No unrestricted shell, MCP, FDX, deployment, publishing, GitHub push,
credential rotation, DNS change, or destructive external operation was used.