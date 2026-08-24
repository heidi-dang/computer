---
name: CodeAct runtime constraints
description: Environment and safety constraints for CPTR's disabled-by-default read-only CodeAct worker.
---

CodeAct workers must keep protocol output separate from captured user output. On this Replit Python runtime, very small address-space limits prevent the launcher from starting; use a bounded platform floor rather than failing valid workers at startup.

**Why:** The worker initially exited before executing code when control messages were redirected or the virtual-memory ceiling was too small.

**How to apply:** Preserve the standalone stdlib-only worker, host-owned JSON-RPC capabilities, AST/import allowlists, wall/CPU/output/call limits, and forced teardown on timeout/cancellation/failure.

Live qualification is model-specific and must be enforced at the production
admission boundary, not merely documented. A qualifying report must cover the
complete native/CodeAct corpus and all named escape categories; partial,
duplicate, malformed, or stale-model reports fail closed.

**Why:** A non-perfect live report must not be bypassed later by flipping the
runtime mode or role allowlist alone.

**How to apply:** Keep CodeAct disabled unless its configured report has the
exact approved model and complete 100% result set. Any provider or generated
program failure is an incorrect observation that preserves a `keep-disabled`
report rather than an exception that hides qualification evidence.