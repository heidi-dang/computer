---
name: Disposable qualification runtime
description: Constraints for isolated CPTR qualification instances and same-origin static serving.
---

Disposable CPTR instances must select their own data directory before importing CPTR modules, must not inherit managed provider credentials implicitly, and must keep the generated frontend build available when the API serves the SPA.

**Why:** CPTR resolves database/config paths at import time, auto-discovers managed provider credentials from environment variables, and the API fallback serves `frontend/build/index.html`; removing generated build output makes the preview blank.

**How to apply:** Start qualification processes with isolated environment and temporary resources, explicitly unset ambient provider variables unless a non-production connection is supplied, and rebuild generated frontend output before restarting the API.

Replay-backed UI state must publish a bounded snapshot atomically after applying a polling response; rapid per-event reactive assignments can observe stale state and collapse a valid multi-frame terminal replay to one visible row.

**Why:** The qualification API returned distinct terminal frame IDs and sequences, but rapid frontend replay merges repeatedly observed a one-item reactive array.

**How to apply:** Keep the synchronous merge accumulator separate from rendered reactive state, then assign the bounded replay snapshot in one update.