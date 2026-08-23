---
name: FlowDeck evidence verifier policy
description: Durable completion evidence must be tied to active physical attempts and database-derived operation identity.
---

Terminal success is accepted only from authoritative runtime/verifier observations, never specialist prose; durable binding rejects mismatched run, operation, step, workspace, owner, or operation fingerprint.

**Why:** A stale physical attempt could otherwise publish completion after a newer attempt began, and caller-supplied identity could make evidence appear to belong to another operation.

**How to apply:** Preserve active-attempt and fencing checks in terminal transitions, keep UNKNOWN/manual review fail-closed, and derive evidence identity from durable records rather than model claims.