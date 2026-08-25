---
name: Live terminal proxy boundary
description: Same-origin protected FlowDeck mutations must see the browser-facing host through the frontend development proxy.
---

FlowDeck same-origin checks must receive the browser-facing Host when requests are proxied from the Svelte frontend; rewriting Host to the backend port causes legitimate authenticated mutations to fail.

**Why:** Browser Origin and proxy-rewritten Host differ across local frontend/backend ports, so protected checkpoint operations were rejected even after real authentication and workspace ownership succeeded.

**How to apply:** Preserve the frontend-facing Host for `/api`, `/v1`, and Socket.IO proxy traffic rather than weakening same-origin validation.