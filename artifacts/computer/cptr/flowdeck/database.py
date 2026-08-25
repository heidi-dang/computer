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
            {"table": row[2], "column": row[3], "references": row[4], "on_update": row[5], "on_delete": row[6]}
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
                tables = schema["tables"]
                result = {"schema": schema, "schema_fingerprint": _fingerprint(schema), "usage": _usage_analysis(root, schema)}
                result["migration_files"] = [str(p.relative_to(root)) for p in _discover_migrations(root)]
                result["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
                result["database"] = str(path.relative_to(root))
                return result
        if request.engine == "postgresql":
            raise DatabaseContractError("PostgreSQL support requires the configured server driver; no credentials are accepted from requests")
        raise DatabaseContractError("database engine must be sqlite or postgresql")

    async def query(self, request: DatabaseRequest, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._query, request, sql, params or [])

    def _query(self, request: DatabaseRequest, sql: str, params: list[Any]) -> dict[str, Any]:
        _query_allowed(sql)
        root = _root(request.workspace)
        if request.engine != "sqlite":
            raise DatabaseContractError("PostgreSQL query execution is unavailable until its server driver is configured")
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
        if request.engine != "sqlite":
            raise DatabaseContractError("PostgreSQL migrations require the configured server driver")
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


project_database = ProjectDatabaseService()