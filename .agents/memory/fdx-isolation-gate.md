---
name: FDX isolation gate
description: Safety requirements for the optional FlowDeck accelerator boundary.
---

FDX must remain disabled unless its execution has both a durable exclusive workspace lease and independently verified OS-level isolation. Protocol claims, snapshots, and observed side-effect cleanup are not sufficient proof of containment; absent proof, native CPTR is the only fallback.

**Why:** A subprocess can self-report read-only behavior while still retaining network, syscall, child-process, or concurrent-workspace hazards that snapshot/restore cannot safely contain.

**How to apply:** Any future FDX change must preserve fail-closed gating, lease ownership before process creation, bounded process-group cleanup, non-authoritative output, and native fallback on every policy or containment failure.