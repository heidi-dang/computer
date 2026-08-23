---
name: FlowDeck coding gates
description: Durable safety rules for enabling FlowDeck mutation specialists.
---

Mutation specialists must be enabled one role at a time and may only use structured, owned-path tools. Each write needs a durable logical intent and physical attempt before execution, an exclusive current workspace lease/fencing epoch, and an independent postcondition verifier afterward. Any interrupted or unverifiable mutation becomes UNKNOWN/manual review rather than a successful run.

**Why:** CPTR remains the sole model/tool execution owner, so policy must be enforced at the native dispatch boundary rather than trusted to specialist prompts or claims.

**How to apply:** Keep generic shell mutation, Git mutation, network writes, secrets, package installation, deployment, publishing, and unrestricted autonomy disabled. Frontend-coder uses the same mutation boundary; browser-debugger is read-only and local-preview-only. Do not enable debug-specialist or devops until their own gates pass.