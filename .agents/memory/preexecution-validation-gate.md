---
name: Heidi pre-execution validation
description: Native durable validation must inspect live workspace and policy before any specialist dispatch.
---

Heidi’s controlled coordinator validates the task against the current workspace facts, selected CPTR model, strict FlowDeck policy, and frozen boundaries before consuming execution budget or creating child work.

**Why:** User requests can contain incorrect assumptions, unavailable capabilities, or conflicts with guarantees that must not be silently reinterpreted into execution.

**How to apply:** Keep validation deterministic, bounded, model-free, and visible through native durable activity events. Rejections and clarifications must finalize before child steps, attempts, tools, models, or workspace mutation; cancellation and reconnect must remain on the existing durable lifecycle.