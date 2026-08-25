# FlowDeck Phase 10 Generated-Application Authentication Acceptance

Status: **IN PROGRESS — NOT ACCEPTED**

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

- [ ] Auth operations have durable evidence, native transcript integration,
  cancellation, recovery, idempotency, stale-attempt fencing, and bounded UI
  status.
- [ ] Disposable authenticated fixtures cover restart, concurrency, repeated
  requests, expiry, cancellation, and late outcomes.
- [ ] Backend, gateway, runtime, browser, database, and FlowDeck regressions
  pass unchanged.
- [x] Frontend typecheck/build, visual regression, Ruff, Python compilation,
  diff/integrity, and security checks pass.

## Scoring

Product behavior: 4.0; security: 3.0; durable FlowDeck integration: 1.5;
regression/quality: 1.5. Any P0/P1 defect fails acceptance regardless of
numeric score.

## Current qualification evidence

- Focused auth/FlowDeck/database/runtime suite: **29 passed, 4 skipped**.
- Full backend regression before the final callback tightening: **246 passed,
  4 skipped, 47 subtests**; rerun the complete suite before acceptance.
- Frontend typecheck and production build: passed.
- Static checks and Python compilation: passed.
- Security scanners: HoundDog clean; dependency and SAST scanners reported
  pre-existing high findings outside this Phase 10 change, so the security gate
  is not accepted.
- External-provider credentials were not available; external adapters remain
  explicitly **unverified**, not mocked as passed.

## Open P1 acceptance defects

1. Generated-auth mutations are not yet represented as durable FlowDeck
   operations with cancellation, recovery, fencing, idempotency replay, native
   transcript events, and authoritative evidence.
2. No real external-provider qualification has been completed.
3. The complete post-change backend and visual qualification reruns are still
   pending.

Phase 10 must remain **not accepted** and Phase 11 must not start until these
defects are closed and the score is at least 9.0/10 with zero P0/P1 defects.