---
name: Realtime smoke harness
description: Constraint for CPTR provider-backed realtime smoke checks in clean installs.
---

The realtime smoke check should use Engine.IO polling over the declared HTTP
client rather than optional Socket.IO transport packages.

**Why:** Python Socket.IO client transports can add undeclared optional
dependencies, making authenticated provider and transcript regressions
unrepeatable in a clean environment.

**How to apply:** Keep the runner cookie/token authenticated, capture terminal
events:chat envelopes, correlate their message IDs with the durable chat
endpoint, and report successful assistant text separately from provider errors.