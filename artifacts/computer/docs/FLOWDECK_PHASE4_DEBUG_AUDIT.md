# Phase 4 Debug Audit: Expired-Session Blank Preview

Status: **completed**  
Scope: Phase 4 `/debug` root-cause workflow only. No later phase was started.

## Reproduction

1. Start the API and web workflows.
2. Open the proxied Computer preview with an expired or invalid `cptr_session` cookie.
3. Allow the initial auth check and the first protected state request to complete.
4. Observe the preview stuck on a blank/transitioning page instead of presenting sign-in.

Observed request sequence:

- `GET /api/auth` returned successfully.
- `GET /api/config` returned successfully.
- A protected state request returned `401`.
- The browser repeatedly re-entered startup instead of reaching the login UI.

## End-to-end trace

### Frontend

`+layout.svelte` owns the startup auth state. Protected API calls use the shared `fetchHandler`.

### API

The FastAPI auth middleware correctly rejected the expired protected request with `401`. This was not a provider, database, or routing failure.

### FlowDeck / child / CPTR / tool / database / realtime

No FlowDeck run, child specialist, CPTR model loop, tool call, database mutation, or realtime transport was involved in the defect. The failure occurred before authenticated application state initialization. This boundary was verified from the API log: the failing request was the protected state endpoint, not a FlowDeck or model endpoint.

## First incorrect state

The first incorrect state was frontend session handling: `fetchHandler` called `clearSession()`, and `clearSession()` immediately forced `window.location.reload()` on a protected `401`. The reload happened before the layout could render its `needs_login` state, creating a startup/reload loop and a blank preview.

## Minimal repair

- Removed the forced full-page reload from `clearSession()`.
- Added a session-expired browser event.
- Added a layout listener that sets `authState = 'needs_login'` and resets `stateLoaded`.
- Left backend authentication, `401` responses, provider discovery, model selection, FlowDeck authority, and CPTR execution unchanged.

## Regression proof

- Frontend typecheck: passed.
- Frontend production build: passed.
- Backend aggregate: **218 passed**, 43 subtests.
- Focused Phase 3 suite: **95 passed**, 5 subtests.
- Browser preview after restart: rendered the Computer sign-in screen.
- Browser console after repair: no JavaScript exception.
- API workflow after restart: healthy.

## Non-masking checks

- No fallback model or alternate transport was added.
- No fake availability or provider bypass was added.
- No FlowDeck or CPTR transcript shortcut was added.
- No credentials, DNS, deployment, MCP, or FDX settings were changed.
- Existing Svelte compiler accessibility/reactivity warnings remain pre-existing warnings; they do not prevent typecheck or production build.

## Phase 4 decision

The defect was reproduced, correlated to its first incorrect frontend state, repaired minimally, and covered by verification. Phase 4 is complete. No later master-plan phase was started.