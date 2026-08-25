---
name: Project database boundary
description: User-project PostgreSQL access must use a distinct explicit server binding, never CPTR's internal database URL.
---

User-project database operations must never reuse CPTR's own internal
`DATABASE_URL`. PostgreSQL requires a separate server-side
`CPTR_PROJECT_DATABASE_URL`; request payloads cannot provide DSNs or
credentials.

**Why:** The CPTR control database and a user's project database have different
ownership and security boundaries. Reusing the internal URL could expose
control-plane data as if it were project data.

**How to apply:** Keep database adapters fail-closed when the dedicated project
binding or isolated fixture is absent, and never report PostgreSQL qualification
without exercising that isolated binding.