# FlowDeck Phase 9 User-Project Database Acceptance

Status: **ACCEPTED — Phase 9 frozen**
Score: **9.4/10**

Phases 1–8 remain frozen. Phase 10 has not started.

## Implemented

- CPTR-native, authenticated FlowDeck database operation routes:
  `inspect`, `query`, `migrate`, and `restore`.
- Canonical workspace ownership resolution before every operation.
- SQLite support with bounded paths, no symlink escape, bounded SQL and rows,
  parameter binding, integrity checks, schema fingerprints, migration-file
  discovery, migration history, snapshots, restore, and idempotent durable
  operation runs/events.
- Schema understanding for tables, columns, nullability, defaults, primary
  keys, foreign keys, indexes, and unique indexes.
- Source usage analysis with table references and possible N+1 heuristics.
- Schema-drift basis through deterministic schema fingerprints.
- Destructive migration denial and unsafe direct `NOT NULL` addition denial.
- Snapshot-before-migration with restoration on failure.
- PostgreSQL adapter path using `psycopg[binary]`, exercised through the
  separately configured server-side `CPTR_PROJECT_DATABASE_URL`.
- PostgreSQL query and migration cancellation through the authenticated
  FlowDeck cancel route, using a server-side operation handle and native
  PostgreSQL connection cancellation.
- Durable PostgreSQL migration checkpoint evidence in the native FlowDeck
  event log. Completed results replay from durable events; interrupted
  operations enter reconciliation/manual-review instead of being retried.
- CPTR’s internal `DATABASE_URL` is explicitly excluded; request bodies cannot
  provide DSNs, credentials, or unrelated database targets.
- Bounded FlowDeck database UI for engine/path, schema, tables, relationships,
  indexes, integrity, fingerprints, and read-only query results.

## Verified

- Real SQLite fixtures: schema, rows, relationships, indexes, integrity,
  migration, snapshot/restore, destructive denial, nullable-risk denial,
  concurrent reads, and workspace-boundary denial.
- Disposable real PostgreSQL fixture: authenticated FlowDeck inspection,
  parameterized reads, relationships, indexes, constraints, concurrent reads,
  transactional migration success, transactional rollback on failure,
  destructive/unsafe denial, idempotent replay, and cleanup passed.
- PostgreSQL long-running query and migration cancellation: interruption,
  rollback, terminal cancellation, repeated cancellation, no late resurrection,
  and cleanup passed.
- Authenticated cancellation race and cancelled idempotency replay passed.
- Focused PostgreSQL cancellation/database/HTTP suite: **9 passed**; combined
  database/HTTP/coding fixture run: **21 passed**.
- Full backend regression with the disposable PostgreSQL binding:
  **245 passed**, 43 subtests.
- Ruff: passed.
- Python compilation: passed.
- Diff/integrity checks: passed.
- Frontend typecheck: **0 errors**.
- Frontend production build: passed.
- Existing visual regression: **16 passed** at desktop and narrow/mobile widths.
- API, web, and component-preview workflows restarted and running.

## Authority and checkpoint semantics

The disposable project binding and fixture exist only for qualification and are
removed afterward. The operation handle is process-local only for interrupting
the active driver connection; the authoritative lifecycle is the durable
FlowDeck run/event state. A cancelled run cannot be restarted by idempotent
replay, and a worker result that arrives after cancellation is discarded.

PostgreSQL checkpoint semantics are transactional: migration execution is one
server transaction, and failure or cancellation rolls back the transaction.
The before/after fingerprints, SQL digest, and checkpoint declaration are
persisted in the native FlowDeck event log. A completed migration replays its
durable result after restart; an active migration without a durable result is
put into reconciliation/manual review rather than guessed successful.

PostgreSQL's verified checkpoint semantics are intentionally transactional:
the migration runs in one server transaction and a failure rolls back the
entire transaction. This is not represented as a filesystem snapshot or as a
fabricated `pg_dump` restore.

No P0/P1 defects remain. No credentials were exposed or invented, no unrelated
database was accessed, and no Phase 10 implementation was started. Phase 9 is
frozen and ready for Phase 10 planning; Phase 10 itself was not started.