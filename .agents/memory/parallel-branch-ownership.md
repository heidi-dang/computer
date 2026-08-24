---
name: Parallel branch ownership
description: Scope rules for concurrent Build mutation branches
---

Concurrent mutation prompts must assign exclusive backend/frontend ownership and explicitly exclude shared metadata, tests, documentation, lockfiles, and generated/cache artifacts.

**Why:** Independent worktrees can still produce reconciliation conflicts when model children interpret a broad objective as permission to edit shared files. Reconciliation must remain fail-closed, but clear ownership prevents avoidable overlap.

**How to apply:** Include concrete branch scope boundaries in each child task and keep shared files in the immutable common base whenever possible.