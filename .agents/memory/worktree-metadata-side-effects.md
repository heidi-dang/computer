---
name: Worktree metadata side effects
description: Avoid false overlap from CPTR metadata helpers during parallel Build branches
---

CPTR metadata helpers must not edit a branch worktree's `.gitignore`; worktree `.git` files point at shared Git metadata, and such edits appear as cross-branch mutations.

**Why:** Live parallel qualification showed that automatic `.cptr` ignore maintenance can create protected-path changes unrelated to the requested branch, blocking otherwise independent authoritative integrations.

**How to apply:** Keep `.cptr` ignore maintenance on canonical repositories only, or ensure it is already present in the immutable common base before creating parallel worktrees.