---
name: FlowDeck browser authentication
description: Authentication boundary between the FlowDeck browser UI and CPTR's programmatic gateway.
---

FlowDeck browser endpoints must authenticate the existing CPTR session cookie, while preserving Bearer-token authentication for programmatic gateway callers.

**Why:** Reusing the gateway's Bearer-only authenticator for browser requests returned 401, and CPTR's shared frontend fetch wrapper correctly interpreted that as an expired session and logged the user out.

**How to apply:** Keep browser FlowDeck authentication in its own request helper. Do not broaden the gateway's Bearer-only auth or expose route-specific model/tool credentials to the frontend.