# FlowDeck Phase 9 User-Project Database Acceptance

Status: **NOT ACCEPTED — internal lifecycle blockers remain**
Score: **8.8/10**

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
- Focused database/HTTP/FlowDeck PostgreSQL suite: **7 passed**; combined
  database/HTTP/coding fixture run: **19 passed**.
- Full backend regression with the disposable PostgreSQL binding:
  **243 passed**, 43 subtests.
- Ruff: passed.
- Python compilation: passed.
- Diff/integrity checks: passed.
- Frontend typecheck: **0 errors**.
- Frontend production build: passed.
- Existing visual regression: **16 passed** at desktop and narrow/mobile widths.
- API, web, and component-preview workflows restarted and running.

## Acceptance blockers

The disposable project binding and fixture now exist only for the qualification
run and were removed afterward. The remaining blockers are internal:

1. Database operations do not yet expose a durable cancellable operation handle
   that can interrupt an in-flight PostgreSQL query/migration and reconcile its
   FlowDeck run after cancellation. Existing FlowDeck cancellation/recovery
   guarantees are covered for native operations, but cannot be attributed to
   this database adapter yet.
2. PostgreSQL migration history/checkpoints are returned as verified
   transactional evidence, but are not yet durably persisted and reconciled
   across a service restart in the same way as SQLite migration history.

PostgreSQL's verified checkpoint semantics are intentionally transactional:
the migration runs in one server transaction and a failure rolls back the
entire transaction. This is not represented as a filesystem snapshot or as a
fabricated `pg_dump` restore.

Accordingly this record remains below the requested 9/10 acceptance gate.
No credentials were exposed or invented, no unrelated database was accessed,
and no Phase 10 work was started.