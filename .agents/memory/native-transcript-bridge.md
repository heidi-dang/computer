---
name: Native transcript bridge
description: FlowDeck specialist output must use the normal CPTR event semantics while durable FlowDeck events remain status and recovery signals.
---

FlowDeck must not maintain a second renderer for specialist chats: remap the child chat identity to Heidi's synthetic assistant and pass deltas, output items, done, errors, cancellation, and shell/tool activity through the same native CPTR handler used by the normal agent.

**Why:** Durable FlowDeck lifecycle events prove orchestration progress but do not contain the specialist's complete native transcript. Intercepting them as the renderer can leave failures or completion stuck at the last lifecycle label.

**How to apply:** Keep Socket.IO as the native stream, use polling only for durable reconciliation/recovery, deduplicate output items by their native IDs/call IDs, and let durable terminal state remain authoritative for orchestration completion. Heidi turns must use real CPTR user/assistant rows; child tasks must be registered with CPTR cancellation and mirror native output into the parent row without creating a second renderer.