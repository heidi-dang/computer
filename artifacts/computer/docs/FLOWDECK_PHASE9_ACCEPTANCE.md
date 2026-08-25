# FlowDeck Phase 9 User-Project Database Acceptance

Status: **NOT ACCEPTED — external qualification blocker**
Score: **8.4/10**

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
- PostgreSQL adapter path using `psycopg[binary]`, but only through the
  separately configured server-side `CPTR_PROJECT_DATABASE_URL`.
- CPTR’s internal `DATABASE_URL` is explicitly excluded; request bodies cannot
  provide DSNs, credentials, or unrelated database targets.
- Bounded FlowDeck database UI for engine/path, schema, tables, relationships,
  indexes, integrity, fingerprints, and read-only query results.

## Verified

- Real SQLite fixtures: schema, rows, relationships, indexes, integrity,
  migration, snapshot/restore, destructive denial, nullable-risk denial,
  concurrent reads, and workspace-boundary denial.
- Focused database/HTTP/coding suite: **35 passed**.
- Full backend regression: **241 passed**, 43 subtests.
- Ruff: passed.
- Python compilation: passed.
- Diff/integrity checks: passed.
- Frontend typecheck: **0 errors**.
- Frontend production build: passed.
- Existing visual regression: **16 passed** at desktop and narrow/mobile widths.
- API, web, and component-preview workflows restarted and running.

## Acceptance blocker

The current workspace has no dedicated `CPTR_PROJECT_DATABASE_URL`, and no
isolated PostgreSQL fixture/database is available. The installed PostgreSQL
adapter therefore correctly fails closed before connecting. PostgreSQL success,
rollback, concurrent access, cancellation, restart/recovery, and adversarial
fixture qualification cannot be claimed without that explicitly provisioned
project database.

Accordingly this record is intentionally not a Phase 9 acceptance record that
passes the requested 9/10 gate. No credentials were exposed or invented, no
unrelated database was accessed, and no Phase 10 work was started.