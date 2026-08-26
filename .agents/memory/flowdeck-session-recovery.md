---
name: FlowDeck session recovery
description: Preserve route-local FlowDeck work across an in-place authentication interruption without carrying request or session data.
---

When authentication expires, FlowDeck route state may be unmounted while the shell shows login. Snapshot only allowlisted composer context (`objective`, `workspace`, and a UI mode) in session-scoped browser storage; keep active-run recovery separate and never persist request, response, credential, or session objects.

The optional “save this draft on this device” path is a separate local-storage record, disabled by default, limited to the same composer context, and bounded by a short expiry plus an explicit discard action. It must never become a general session, run, response, or credential cache.

**Why:** Session recovery is needed for in-place re-login, while cross-tab retention is a deliberate privacy tradeoff that must not silently expand what the browser keeps.

**How to apply:** Keep the two storage lifetimes and keys independent; only create the longer-lived record after explicit opt-in, delete it on run start/reset or discard, and reject expired or malformed records before rehydration.

**Why:** The shell intentionally recovers authentication without a full reload, so route-local drafts otherwise disappear exactly when users need to sign back in. Mixing draft and owned-run records can also relaunch or expose stale run data.

**How to apply:** Subscribe the FlowDeck route to the shared session-expired event before route teardown, rehydrate the draft only in composer mode, prefer an owned run when one exists, and assert that re-login restores fields without another create request.