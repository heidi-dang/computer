---
name: PostgreSQL cancellation and reconciliation
description: Project database operations use native driver cancellation with durable FlowDeck terminal authority
---

PostgreSQL project-database cancellation must interrupt the active driver connection, but the durable FlowDeck run/event state remains the authority for terminal status, replay, and recovery.

**Why:** A worker can observe cancellation after the cancel request has already committed the terminal run state; allowing that worker to write or replay its result would resurrect cancelled work.

**How to apply:** Persist migration checkpoint evidence in native FlowDeck events, discard late outcomes, reject cancelled idempotency replays, and route interrupted active operations to reconciliation/manual review rather than guessing success.