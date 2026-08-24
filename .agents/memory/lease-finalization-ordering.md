---
name: Lease finalization ordering
description: Workspace lease requirements for fenced child completion
---

Authoritative attempt, operation, step, and run completion must happen while the child still holds its workspace lease; release is the final cleanup action.

**Why:** The durable completion path rechecks the lease fence. Releasing first turns a valid child result into `StaleWriterError`, leaving the child lifecycle inconsistent and forcing manual review.

**How to apply:** For every leased tester or coding child, keep the lease through successful and failed finalization, release in a final cleanup block afterward, and release explicitly on interruption paths.