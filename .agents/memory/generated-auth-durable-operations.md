---
name: Generated-auth durable operations
description: Generated-app authentication actions use the shared FlowDeck lifecycle and verifier evidence.
---

Generated-app signup, sign-in, sign-out, callback verification, and session inspection must be represented by the shared FlowDeck run/step/operation/attempt lifecycle. Public evidence may include user-safe results, but must never include session or CSRF secrets.

**Why:** Authentication side effects need replay-safe idempotency and authoritative recovery semantics just like other controlled operations, while browser clients still need a simple synchronous response.

**How to apply:** Persist intent before invoking the auth adapter, finish attempts with verifier-bound evidence, broadcast lifecycle events through the native FlowDeck event channel, and expose owner/workspace-scoped status and cancellation.