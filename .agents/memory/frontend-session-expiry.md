---
name: Frontend session expiry
description: Protected API 401s must transition the Svelte shell to authentication without a reload loop.
---

When a protected request returns 401, the frontend should clear session state and render the login screen in place. Forcing a full reload can loop through startup checks and leave the preview blank, especially when the browser holds an expired session cookie.

**Why:** The preview became a blank page after a protected preferences request failed, even though the backend and auth endpoints were healthy.

**How to apply:** Keep session-expiry handling state-driven; verify both an unauthenticated first load and an expired-session transition in the proxied preview.