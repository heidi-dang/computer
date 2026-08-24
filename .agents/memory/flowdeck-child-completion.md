---
name: FlowDeck child completion
description: Native transcript terminal-event ownership for FlowDeck child runs
---

Child FlowDeck native events may share the parent Heidi transcript, but a child `done` event must never end parent streaming or move the parent into durable verification. Only a completion event belonging to the parent run may do that.

**Why:** Child runs emit native-looking completion events with both their own run identity and the parent run identity. Treating any `done` event as parent completion makes the UI appear stalled while the durable coordinator is still running.

**How to apply:** Preserve child output/activity in the shared transcript, but guard terminal handling by requiring the event not to identify a distinct child run.