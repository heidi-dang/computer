# FlowDeck Phase 10 Generated-Application Authentication Acceptance

Status: **IN PROGRESS**

Phases 1–9 remain frozen. Phase 11 is prohibited unless this record reaches
at least **9.0/10** with zero open P0/P1 defects.

## Product gate

- [ ] Existing auth detection recognizes supported Auth.js, Clerk, Supabase
  Auth, Firebase Auth, OAuth/OIDC, and native/custom markers.
- [ ] Existing recognized auth is preserved; ambiguous and unknown providers
  fail closed.
- [ ] Bounded native/local adapter supports signup where applicable, sign-in,
  sign-out, protected access, reload persistence, expiry, roles, and denial.
- [ ] External-provider-style adapter requires server-owned verifier config and
  stays explicitly unverified without real credentials.
- [ ] Redirect and callback handling is exact-origin, state/nonce/PKCE-aware
  where applicable, and rejects unsafe redirects.

## Security gate

- [ ] Passwords are never stored directly; session tokens are opaque and
  stored hashed.
- [ ] Cookies have HttpOnly, SameSite, bounded expiry, and Secure in
  production; CSRF is enforced for cookie mutations where applicable.
- [ ] CORS and same-origin boundaries are enforced.
- [ ] Authorization is server-side and role checks deny unauthorized access.
- [ ] Client-supplied provider authority, credentials, claims, DSNs, and
  callback verification settings are rejected.
- [ ] Secrets never enter logs, transcripts, events, browser storage, or
  responses.

## FlowDeck and regression gate

- [ ] Auth operations have durable evidence, native transcript integration,
  cancellation, recovery, idempotency, stale-attempt fencing, and bounded UI
  status.
- [ ] Disposable authenticated fixtures cover restart, concurrency, repeated
  requests, expiry, cancellation, and late outcomes.
- [ ] Backend, gateway, runtime, browser, database, and FlowDeck regressions
  pass unchanged.
- [ ] Frontend typecheck/build, visual regression, Ruff, Python compilation,
  diff/integrity, and security checks pass.

## Scoring

Product behavior: 4.0; security: 3.0; durable FlowDeck integration: 1.5;
regression/quality: 1.5. Any P0/P1 defect fails acceptance regardless of
numeric score.