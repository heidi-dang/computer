---
name: Live build provider checks
description: Runtime prerequisites and failure interpretation for authenticated live /build smoke tests.
---

Live /build smoke tests depend on both the controlled mutation flag and a healthy configured model connection. A successful API submission, planning children, or steering acknowledgement does not prove a build; provider authentication or availability failures must leave the mutation attempt unknown and prevent completion.

**Why:** The Replit-hosted CPTR runtime can retain stale or blocked provider connections even while the API, owned-workspace checks, and read-only specialists are healthy.

**How to apply:** Enable mutation only for the smoke run, choose a model backed by a reachable configured connection, and inspect durable attempt evidence plus API events before declaring success. Keep cancellation and reload checks separate from the mutation result.