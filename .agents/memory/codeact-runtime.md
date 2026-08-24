---
name: CodeAct runtime constraints
description: Environment and safety constraints for CPTR's disabled-by-default read-only CodeAct worker.
---

CodeAct workers must keep protocol output separate from captured user output. On this Replit Python runtime, very small address-space limits prevent the launcher from starting; use a bounded platform floor rather than failing valid workers at startup.

**Why:** The worker initially exited before executing code when control messages were redirected or the virtual-memory ceiling was too small.

**How to apply:** Preserve the standalone stdlib-only worker, host-owned JSON-RPC capabilities, AST/import allowlists, wall/CPU/output/call limits, and forced teardown on timeout/cancellation/failure.