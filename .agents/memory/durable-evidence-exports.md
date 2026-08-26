---
name: Durable evidence exports
description: Cross-worker stability boundary for downloadable FlowDeck evidence reports
---

Downloadable evidence reports must be derived only from shared durable FlowDeck events. Process-local terminal observer buffers belong to live status/reconnect views, not exported evidence.

**Why:** A worker replacement can discard in-memory observer frames while the shared database remains available, causing the same owned report to change across a restart boundary.

**How to apply:** Keep export audit events and report inputs in the durable store; use process-local terminal frames only for live orchestration responses unless they are explicitly persisted first.

Keyed report downloads are a two-phase audit boundary: reserve the export as
`response_delivery_unknown`, then append transport delivery only after the
response body is handed off. Reusing the key records a retry/replay outcome
without creating another export.

**Why:** An export reservation commits before HTTP delivery and a worker can
stop in that gap; claiming success would misrepresent what the client saw.

**How to apply:** Browser callers should send a stable idempotency key for one
download attempt and retain it when retrying after a transport failure.