---
name: FlowDeck read-only execution
description: Durable constraints for FlowDeck specialist execution and migration evolution.
---

Read-only specialists must use a runtime-enforced allowlist and path containment
guard at CPTR's tool-dispatch boundary; prompt instructions alone are not a
security control. They must not acquire the exclusive workspace mutation lease,
so concurrent investigations can share the same owned workspace safely.

**Why:** Sharing the mutation lease caused read-only concurrency to serialize and
allowed one investigation to fence another unnecessarily. Separating read-only
attempts from mutator fencing preserves concurrency while keeping mutation
leases available for future coding stages.

**How to apply:** Pass the restricted tool set through CPTR's existing
`run_chat_task` path, filter schemas and dispatch (including external tools and
artifact injection), and require runtime/verifier evidence before durable
completion.

Never append new tables to an Alembic revision that is already applied. Create a
new revision for later FlowDeck state such as approvals.

**Why:** Editing an applied revision left an existing SQLite database unable to
upgrade consistently with fresh databases.

**How to apply:** Check the current Alembic head before schema changes and add a
new sequential migration for every new durable table.