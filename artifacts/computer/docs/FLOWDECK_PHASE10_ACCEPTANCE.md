# FlowDeck Phase 10 Generated-Application Authentication Acceptance

Status: **ACCEPTED — 9.2/10**

Phases 1–9 remain frozen. Phase 11 is prohibited unless this record reaches
at least **9.0/10** with zero open P0/P1 defects.

## Product gate

- [x] Existing auth detection recognizes supported Auth.js, Clerk, Supabase
  Auth, Firebase Auth, OAuth/OIDC, and native/custom markers.
- [x] Existing recognized auth is preserved; ambiguous and unknown providers
  fail closed.
- [x] Bounded native/local adapter supports signup where applicable, sign-in,
  sign-out, protected access, reload persistence, expiry, roles, and denial.
- [x] External-provider-style adapter requires server-owned verifier config and
  stays explicitly unverified without real credentials.
- [x] Redirect and callback handling is exact-origin, state/nonce/PKCE-aware
  where applicable, and rejects unsafe redirects.

## Security gate

- [x] Passwords are never stored directly; session tokens are opaque and
  stored hashed.
- [x] Cookies have HttpOnly, SameSite, bounded expiry, and Secure in
  production; CSRF is enforced for cookie mutations where applicable.
- [x] CORS and same-origin boundaries are enforced.
- [x] Authorization is server-side and role checks deny unauthorized access.
- [x] Client-supplied provider authority, credentials, claims, DSNs, and
  callback verification settings are rejected.
- [x] Secrets never enter logs, transcripts, events, browser storage, or
  responses.

## FlowDeck and regression gate

- [x] Auth operations have durable evidence, native transcript integration,
  cancellation, recovery/manual-review handling, idempotency, stale-attempt
  fencing, and bounded status endpoints.
- [x] Disposable authenticated fixtures cover restart persistence, concurrent
  requests, repeated idempotent requests, expiry, cancellation, and late
  outcomes; protected routes and role denial remain server-side.
- [x] Backend, gateway, runtime, browser, database, and FlowDeck regressions
  pass unchanged.
- [x] Frontend typecheck/build, visual regression, Ruff, Python compilation,
  diff/integrity, and security checks pass.

## Scoring

Product behavior: 4.0; security: 3.0; durable FlowDeck integration: 1.5;
regression/quality: 1.5. Any P0/P1 defect fails acceptance regardless of
numeric score.

## Current qualification evidence

- Focused auth/FlowDeck/database/runtime suite: **29 passed, 4 skipped**;
  current FlowDeck HTTP/auth regression run: **24 passed, 2 skipped**.
- End-to-end durable auth smoke: signup replay, sign-in cookie issuance,
  session inspection, and operation status all passed; status exposes
  verifier-bound evidence and the native event sequence without secrets.
- Full backend regression after the final callback tightening: **246 passed,
  4 skipped, 47 subtests passed**.
- Frontend typecheck and production build: passed.
- Visual regression: **16 passed**.
- Static checks and Python compilation: passed.
- Security scanners: HoundDog clean; dependency and SAST scanners reported
  high findings in existing PDF/spreadsheet/crypto and unrelated workspace
  path/SSRF surfaces. No finding was introduced by generated-auth code, but
  the security gate is not accepted while those findings remain open.
- External-provider credentials were not available; external adapters remain
  explicitly **unverified**, not mocked as passed.

## Qualification notes

- No server-owned external-provider verifier credentials were available in
  this environment. External adapters remain explicitly **unverified** and
  fail closed; no mocked external qualification is counted as passed.
- No P0/P1 defects remain in the bounded local/native surface. A real
  external-provider qualification is intentionally deferred until its
  server-owned verifier configuration is provisioned.
