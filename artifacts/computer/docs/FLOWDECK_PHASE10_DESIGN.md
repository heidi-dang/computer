# FlowDeck Phase 10 Generated-Application Authentication

Status: **CANONICAL DESIGN — implementation authorized**

## Scope

CPTR/Heidi may inspect an existing workspace's generated-application
authentication and preserve it when recognized. When no supported auth is
detected, it may configure the bounded native/local adapter. Supported
families are Auth.js, Clerk, Supabase Auth, Firebase Auth, generic OAuth/OIDC,
and bounded native/custom auth.

This is generated-application auth, not a replacement for CPTR control-plane
authentication. Every inspection, configuration, test, and lifecycle operation
is authorized by the existing CPTR authenticated workspace gateway.

## Authority and boundaries

- CPTR remains the only model, browser, tool, filesystem, and execution
  authority. No second model or agent loop is introduced.
- FlowDeck owns durable operation lifecycle, evidence, cancellation, recovery,
  idempotency, and bounded status reporting.
- Provider identity and verifier configuration are server-owned. Browser
  claims, provider names, DSNs, credentials, callback payloads, and redirect
  targets are never authoritative.
- Existing detected auth is preserved; ambiguous or unknown detection fails
  closed rather than replacing or silently selecting a provider.
- Provider families without server-owned verifier configuration are
  `unverified` and cannot claim successful external-provider qualification.
- No deployment, publishing, MCP, FDX, unrestricted shell, credential
  rotation, DNS, or destructive external operation is in scope.

## Adapter contract

Adapters expose discovery metadata, signup where supported, sign-in, sign-out,
session lookup, protected access, role authorization, expiry, and callback
validation. Local/native auth stores only password hashes and hashed opaque
session tokens in workspace-local `.cptr` state. Sessions use separate
HttpOnly, Secure-in-production, SameSite cookies with bounded expiry.

External adapters are configuration descriptors plus fail-closed verifier
requirements. Callback validation requires a server-owned issuer, audience,
JWKS/verification configuration, exact registered redirect, state, nonce, and
PKCE where applicable. Open redirects and client-supplied verifier settings are
denied.

## Durable behavior

Auth inspect/configure/qualification operations use native FlowDeck durable
runs and events, idempotency keys, authoritative verifier evidence, and the
existing cancellation/recovery rules. Cancelled runs cannot replay or
resurrect, late results are discarded, and interrupted work becomes
reconciliation/manual review rather than guessed success. Native transcript
events remain the UI/status source.

## Qualification fixtures

Qualification must use disposable workspace fixtures: one real session-based
local/native adapter and one external-provider-style descriptor. The latter
passes only when real server-owned verifier credentials/configuration are
available; otherwise it remains explicitly unverified.