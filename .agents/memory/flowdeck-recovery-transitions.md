---
name: FlowDeck recovery transitions
description: Durable state-machine rules for interrupted FlowDeck operations.
---

Interrupted operations may move from RUNNING to OUTCOME_UNKNOWN and then to MANUAL_REVIEW_REQUIRED, but terminal attempts cannot be reclassified and manual review cannot be silently reconciled back into success or failure.

**Why:** A late cancellation, stale worker, or unverifiable verifier result must never overwrite a positively observed outcome or bypass explicit human review.

**How to apply:** Keep durable transitions fail-closed, require authoritative evidence for successful/failed operations, and treat an explicit manual-review run as a terminal recorded outcome rather than an implicit retry.