---
name: Native cancellation propagation
description: Cancellation semantics across CPTR native loops and FlowDeck child boundaries
---

Native CPTR loops may persist user-visible cancellation cleanup, but they must re-raise `CancelledError` afterward whenever an enclosing durable child owns timeout, fencing, or UNKNOWN classification.

**Why:** Consuming cancellation makes a timed-out or stopped child look like a successful native return, allowing false authoritative success before the parent can apply its fail-closed lifecycle transition.

**How to apply:** Treat cleanup and propagation as separate responsibilities: finalize the chat stream and registries, then re-raise; let the FlowDeck child boundary classify timeout/stop and finalize durable state.