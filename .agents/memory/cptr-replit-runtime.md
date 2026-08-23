---
name: CPTR Replit runtime layout
description: Why CPTR uses a static web artifact plus a dynamic API artifact on Replit.
---

CPTR’s source remains one repository and one product, but Replit publishing requires its Svelte static site and its long-running FastAPI/Socket.IO runtime to be exposed by separate managed services.

**Why:** The web artifact production schema only serves static output, while CPTR must keep ASGI, Socket.IO, terminal WebSockets, and gateway routes running. Its original single-process launcher remains usable locally; the managed split is a deployment boundary, not an application rewrite.

**How to apply:** Keep the frontend rooted at `/` and route `/api`, `/v1`, and `/socket.io` to the CPTR runtime service. Preserve CPTR’s SQLite data directory with the project and keep its Vite backend target environment-derived.