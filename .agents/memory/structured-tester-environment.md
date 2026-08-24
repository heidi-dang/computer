---
name: Structured tester environment
description: Environment boundary for CPTR-aware verifier subprocesses
---

Structured tester subprocesses should receive only their bounded execution environment plus the explicit CPTR data-directory setting needed to reach the authenticated parent control-plane database.

**Why:** A tester that loses the parent data-directory setting can open a different empty database and leave durable build nodes waiting indefinitely; inheriting the full environment would risk secrets and unrelated authority.

**How to apply:** Add narrowly allowlisted CPTR runtime variables when present; never pass the ambient environment wholesale.