---
name: Terminal execution observer
description: Live Terminal observes CPTR execution frames directly while FlowDeck owns structured lifecycle activity.
---

Live Terminal should consume bounded, safe observer frames emitted at CPTR execution boundaries through the existing authenticated event emitter. Native transcript deltas and output items remain transcript rendering data, not the terminal's primary source.

**Why:** Transcript events can be visible while tool execution is already underway, yet ownership and event-shape filtering can leave a terminal renderer waiting or empty.

**How to apply:** Keep one Socket.IO path and one execution authority; frame command/action start, output, exit, identity, sequence, and timestamp at execution sources, redact before emission, bound per-run memory, preserve parent ownership, and let FlowDeck lifecycle records cover validation, planning, specialists, and verification.