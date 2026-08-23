---
name: FlowDeck SQLite concurrency
description: The concurrency boundary and why event ordering is serialized.
---

The first FlowDeck durable deployment is single-worker. Short SQLite transactions
use process-level serialization for per-run event sequence allocation, with busy
retries for brief connection contention. A multi-worker deployment needs a
separately reviewed allocator and lease protocol rather than assuming MAX(sequence)
is safe across workers.

**Why:** SQLite can coordinate the durable records, but a MAX(sequence)+1 event
allocator is only safe when writers are serialized; stale or concurrent writers
must not silently create duplicate event versions.

**How to apply:** Keep orchestration ownership single-worker until the runtime
qualification work explicitly designs and tests multi-worker event allocation,
leases, and migrations.