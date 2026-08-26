---
name: Browser storage regression fixtures
description: Keep browser tests for storage-clearing behavior from reseeding state during reloads.
---

When a browser regression verifies that a storage value stays deleted after reload, seed the value once before the first app load rather than from an init script that runs on every document.

**Why:** Playwright init scripts run again during `page.reload()`. Reseeding there can make a correctly cleared value appear to survive, or can hide a bug in the app's reload behavior.

**How to apply:** Load the fixture, seed local/session storage through the page, reload to rehydrate the seeded state, perform the mutation, and reload again to verify the final state.