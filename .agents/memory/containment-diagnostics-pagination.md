---
name: Containment diagnostics pagination
description: Stable cursor and refresh behavior for the bounded FDX containment diagnostics list.
---

Containment diagnostics pagination must order by descending creation time with a unique ID tie-breaker, and older-page cursors must encode both values. The client should retain already-loaded rows, deduplicate by diagnostic ID, and append older results rather than replacing or resorting visible history.

**Why:** Multiple events can share a millisecond timestamp, and periodic refreshes must not make an operator lose their place or see duplicate/reordered history while investigating.

**How to apply:** Keep category selection and the current older-page cursor as one client state boundary. Reset both when the category changes; refresh the newest page in place without discarding loaded older rows.