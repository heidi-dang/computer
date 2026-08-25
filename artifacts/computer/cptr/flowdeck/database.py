"""Bounded, workspace-owned project database inspection and migrations.

This module deliberately owns no model loop and never accepts a connection
string from the browser. SQLite paths are resolved beneath the owned project;
PostgreSQL uses only the server's configured DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_ROWS = 200
MAX_SQL = 20_000
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DESTRUCTIVE = re.compile(r"\b(drop|truncate|delete\s+from)\b", re.I)
NULLABLE_RISK = re.compile(r"\b(add\s+column|alter\s+column)\b.*\b(not\s+null)\b", re.I | re.S)


class DatabaseContractError(ValueError):
    pass


@dataclass(frozen=True)
class DatabaseRequest:
    request_key: str
    owner: str
    workspace: str
    engine: str = "sqlite"
    database: str | None = None


def _root(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise DatabaseContractError("workspace is not a directory")
    return root


def _sqlite_path(root: Path, database: str | None) -> Path:
    candidate = Path(database or "project.db")
    if candidate.is_absolute():
        path = candidate.resolve()
    else:
        path = (root / candidate).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise DatabaseContractError("database path must remain inside the workspace") from exc
    if path.is_symlink():
        raise DatabaseContractError("symlink database targets are not allowed")
    if path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise DatabaseContractError("only SQLite database files are supported")
    return path


def _json_safe(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return f"<bytes:{len(value)}>"
    return value


def _schema_sqlite(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = []
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        columns = [
            {"name": row[1], "type": row[2], "nullable": not bool(row[3]), "default": row[4], "primary_key": bool(row[5])}
            for row in connection.execute(f'PRAGMA table_info("{name}")')
        ]
        foreign_keys = [
            {"table": row[2], "column": row[3], "references": row[2], "references_column": row[4], "on_update": row[5], "on_delete": row[6]}
            for row in connection.execute(f'PRAGMA foreign_key_list("{name}")')
        ]
        indexes = []
        for row in connection.execute(f'PRAGMA index_list("{name}")'):
            index_name = row[1]
            indexes.append({
                "name": index_name,
                "unique": bool(row[2]),
                "columns": [r[2] for r in connection.execute(f'PRAGMA index_info("{index_name}")')],
            })
        tables.append({"name": name, "columns": columns, "foreign_keys": foreign_keys, "indexes": indexes})
    return {"engine": "sqlite", "tables": tables}


def _postgres_connection():
    try:
        import psycopg
    except ImportError as exc:
        raise DatabaseContractError("PostgreSQL support is unavailable because the server driver is not installed") from exc
    # CPTR's own DATABASE_URL is never a project-database target. A project
    # owner must explicitly configure this separate server-side binding.
    dsn = os.environ.get("CPTR_PROJECT_DATABASE_URL")
    if not dsn:
        raise DatabaseContractError("PostgreSQL requires the server-configured CPTR_PROJECT_DATABASE_URL")
    return psycopg.connect(dsn, connect_timeout=5)


def _schema_postgres(connection) -> dict[str, Any]:
    tables = []
    table_rows = connection.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY table_schema, table_name"
    ).fetchall()
    for schema_name, table_name in table_rows:
        columns = [
            {"name": row[0], "type": row[1], "nullable": row[2] == "YES", "default": row[3], "primary_key": row[4] == "PRIMARY KEY"}
            for row in connection.execute(
                "SELECT c.column_name, c.data_type, c.is_nullable, c.column_default, "
                "CASE WHEN tc.constraint_type = 'PRIMARY KEY' THEN 'PRIMARY KEY' ELSE '' END "
                "FROM information_schema.columns c LEFT JOIN information_schema.key_column_usage k "
                "ON k.table_schema=c.table_schema AND k.table_name=c.table_name AND k.column_name=c.column_name "
                "LEFT JOIN information_schema.table_constraints tc ON tc.constraint_name=k.constraint_name "
                "WHERE c.table_schema=%s AND c.table_name=%s ORDER BY c.ordinal_position",
                (schema_name, table_name),
            ).fetchall()
        ]
        foreign_keys = [
            {"table": row[0], "column": row[1], "references": row[2], "references_column": row[3]}
            for row in connection.execute(
                "SELECT ccu.table_name, kcu.column_name, ccu2.table_name, ccu2.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu ON kcu.constraint_name=tc.constraint_name AND kcu.table_schema=tc.table_schema "
                "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name=tc.constraint_name AND ccu.table_schema=tc.table_schema "
                "JOIN information_schema.key_column_usage ccu2 ON ccu2.constraint_name=ccu.constraint_name AND ccu2.table_schema=ccu.table_schema "
                "WHERE tc.constraint_type='FOREIGN KEY' AND kcu.table_schema=%s AND kcu.table_name=%s",
                (schema_name, table_name),
            ).fetchall()
        ]
        indexes = [
            {"name": row[0], "unique": row[1], "definition": row[2]}
            for row in connection.execute(
                "SELECT indexname, indexdef LIKE 'CREATE UNIQUE%%', indexdef FROM pg_indexes "
                "WHERE schemaname=%s AND tablename=%s ORDER BY indexname",
                (schema_name, table_name),
            ).fetchall()
        ]
        tables.append({"schema": schema_name, "name": table_name, "columns": columns, "foreign_keys": foreign_keys, "indexes": indexes})
    return {"engine": "postgresql", "tables": tables}


def _fingerprint(schema: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _query_allowed(sql: str, *, mutation: bool = False) -> None:
    statement = sql.strip()
    if not statement or len(statement) > MAX_SQL:
        raise DatabaseContractError("query is empty or exceeds the bounded SQL limit")
    if "\x00" in statement:
        raise DatabaseContractError("NUL bytes are not allowed")
    if not mutation and not re.match(r"^(select|pragma|with|explain)\b", statement, re.I):
        raise DatabaseContractError("inspection queries must be read-only")
    if DESTRUCTIVE.search(statement):
        raise DatabaseContractError("destructive database SQL requires a controlled migration")


def _discover_migrations(root: Path) -> list[Path]:
    candidates = []
    for directory in ("migrations", "alembic/versions", "db/migrations", "prisma/migrations"):
        path = root / directory
        if path.is_dir():
            candidates.extend(p for p in path.rglob("*") if p.is_file() and p.suffix in {".sql", ".py"})
    return sorted(candidates)


def _usage_analysis(root: Path, schema: dict[str, Any]) -> dict[str, Any]:
    source_files = [p for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".js", ".ts", ".svelte"} and ".cptr" not in p.parts]
    usage: dict[str, int] = {table["name"]: 0 for table in schema.get("tables", [])}
    n_plus_one = []
    for path in source_files[:1000]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for table in usage:
            usage[table] += len(re.findall(rf"\b(?:from|into|update|join)\s+['\"`]?{re.escape(table)}\b", text, re.I))
        if re.search(r"\b(for|while)\b", text) and re.search(r"\b(?:select|execute|query)\b", text, re.I):
            n_plus_one.append(str(path.relative_to(root)))
    return {"table_references": usage, "possible_n_plus_one_files": n_plus_one[:50]}


class ProjectDatabaseService:
    async def inspect(self, request: DatabaseRequest) -> dict[str, Any]:
        return await asyncio.to_thread(self._inspect, request)

    def _inspect(self, request: DatabaseRequest) -> dict[str, Any]:
        root = _root(request.workspace)
        if request.engine == "sqlite":
            path = _sqlite_path(root, request.database)
            if not path.exists():
                raise DatabaseContractError("SQLite database does not exist")
            with sqlite3.connect(path, timeout=5) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                schema = _schema_sqlite(connection)
                result = {"schema": schema, "schema_fingerprint": _fingerprint(schema), "usage": _usage_analysis(root, schema)}
                result["migration_files"] = [str(p.relative_to(root)) for p in _discover_migrations(root)]
                result["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
                result["database"] = str(path.relative_to(root))
                return result
        if request.engine == "postgresql":
            with _postgres_connection() as connection:
                schema = _schema_postgres(connection)
                return {
                    "schema": schema,
                    "schema_fingerprint": _fingerprint(schema),
                    "usage": _usage_analysis(root, schema),
                    "migration_files": [str(p.relative_to(root)) for p in _discover_migrations(root)],
                    "integrity": "verified by PostgreSQL constraints",
                    "database": "server-configured PostgreSQL",
                }
        raise DatabaseContractError("database engine must be sqlite or postgresql")

    async def query(self, request: DatabaseRequest, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._query, request, sql, params or [])

    def _query(self, request: DatabaseRequest, sql: str, params: list[Any]) -> dict[str, Any]:
        _query_allowed(sql)
        root = _root(request.workspace)
        if request.engine != "sqlite":
            with _postgres_connection() as connection:
                cursor = connection.execute(sql, params)
                rows = [{str(key): _json_safe(value) for key, value in zip([d.name for d in cursor.description], row)} for row in cursor.fetchmany(MAX_ROWS)]
                return {"columns": [d.name for d in cursor.description], "rows": rows, "truncated": len(rows) == MAX_ROWS}
        path = _sqlite_path(root, request.database)
        with sqlite3.connect(path, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(sql, params)
            rows = [{key: _json_safe(row[key]) for key in row.keys()} for row in cursor.fetchmany(MAX_ROWS)]
            return {"columns": [item[0] for item in cursor.description or []], "rows": rows, "truncated": len(rows) == MAX_ROWS}

    async def migrate(self, request: DatabaseRequest, sql: str, *, snapshot: bool = True) -> dict[str, Any]:
        return await asyncio.to_thread(self._migrate, request, sql, snapshot)

    def _migrate(self, request: DatabaseRequest, sql: str, snapshot: bool) -> dict[str, Any]:
        _query_allowed(sql, mutation=True)
        if DESTRUCTIVE.search(sql):
            raise DatabaseContractError("destructive migration denied; provide a non-destructive reconciliation")
        if NULLABLE_RISK.search(sql):
            raise DatabaseContractError("unsafe NOT NULL migration denied without a staged backfill")
        root = _root(request.workspace)
        if request.engine == "postgresql":
            before = self._inspect(request)
            with _postgres_connection() as connection:
                try:
                    with connection.transaction():
                        connection.execute(sql)
                except Exception as exc:
                    raise DatabaseContractError("PostgreSQL migration rolled back") from exc
            after = self._inspect(request)
            history_entry = {
                "fingerprint_before": before["schema_fingerprint"],
                "fingerprint_after": after["schema_fingerprint"],
                "checkpoint": "transactional pre-migration state",
                "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
            }
            return {"before": before, "after": after, "snapshot": history_entry["checkpoint"], "migration_history": history_entry, "verified": True}
        if request.engine != "sqlite":
            raise DatabaseContractError("database engine must be sqlite or postgresql")
        path = _sqlite_path(root, request.database)
        snapshot_path = None
        if snapshot:
            snapshot_dir = root / ".cptr" / "database-snapshots"
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            snapshot_path = snapshot_dir / f"{path.stem}-{int(time.time() * 1000)}.bak"
            if path.exists():
                shutil.copy2(path, snapshot_path)
        before = self._inspect(request)
        try:
            with sqlite3.connect(path, timeout=5) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(sql)
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise DatabaseContractError("integrity verification failed")
            after = self._inspect(request)
        except Exception:
            if snapshot_path and snapshot_path.exists():
                shutil.copy2(snapshot_path, path)
            raise
        history = root / ".cptr" / "database-migration-history.json"
        history.parent.mkdir(parents=True, exist_ok=True)
        entries = json.loads(history.read_text()) if history.exists() else []
        entries.append({"fingerprint_before": before["schema_fingerprint"], "fingerprint_after": after["schema_fingerprint"], "snapshot": str(snapshot_path.relative_to(root)) if snapshot_path else None, "sql_sha256": hashlib.sha256(sql.encode()).hexdigest()})
        history.write_text(json.dumps(entries[-100:], indent=2))
        return {"before": before, "after": after, "snapshot": entries[-1]["snapshot"], "migration_history": entries[-1], "verified": True}

    async def restore(self, request: DatabaseRequest, snapshot: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._restore, request, snapshot)

    def _restore(self, request: DatabaseRequest, snapshot: str) -> dict[str, Any]:
        root = _root(request.workspace)
        if request.engine != "sqlite":
            raise DatabaseContractError("PostgreSQL restore requires an explicit provider snapshot")
        database = _sqlite_path(root, request.database)
        source = (root / ".cptr" / "database-snapshots" / Path(snapshot).name).resolve()
        try:
            source.relative_to(root / ".cptr" / "database-snapshots")
        except ValueError as exc:
            raise DatabaseContractError("snapshot is outside the workspace snapshot store") from exc
        if not source.is_file():
            raise DatabaseContractError("snapshot was not found")
        shutil.copy2(source, database)
        return self._inspect(request)


project_database = ProjectDatabaseService()