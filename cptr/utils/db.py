"""Async database engine, SQLite tuning, and session management."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cptr.env import (
    DATA_DIR,
    DB_BUSY_TIMEOUT_MS,
    DB_CACHE_SIZE_KIB,
    DB_FILE,
    DB_MMAP_SIZE_BYTES,
    DB_SYNCHRONOUS,
    DB_WAL_AUTOCHECKPOINT_PAGES,
)

_engine = None
_async_session = None


def _configure_engine(engine) -> None:
    """Apply safe SQLite connection settings and low-overhead query telemetry."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
            cursor.execute(f"PRAGMA synchronous={DB_SYNCHRONOUS}")
            cursor.execute(f"PRAGMA cache_size=-{DB_CACHE_SIZE_KIB}")
            cursor.execute(f"PRAGMA wal_autocheckpoint={DB_WAL_AUTOCHECKPOINT_PAGES}")
            cursor.execute("PRAGMA temp_store=MEMORY")
            if DB_MMAP_SIZE_BYTES:
                cursor.execute(f"PRAGMA mmap_size={DB_MMAP_SIZE_BYTES}")
        finally:
            cursor.close()

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before_cursor_execute(_conn, _cursor, _statement, _parameters, context, _executemany):
        context._cptr_started_at = time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after_cursor_execute(_conn, _cursor, _statement, _parameters, context, _executemany):
        started = getattr(context, "_cptr_started_at", None)
        if started is None:
            return
        try:
            from cptr.services.runtime_metrics import runtime_metrics

            runtime_metrics.observe_db_query((time.perf_counter() - started) * 1000.0)
        except Exception:
            pass

    @event.listens_for(engine.sync_engine, "handle_error")
    def _handle_error(exception_context) -> None:
        context = getattr(exception_context, "execution_context", None)
        started = getattr(context, "_cptr_started_at", None) if context is not None else None
        try:
            from cptr.services.runtime_metrics import runtime_metrics

            duration_ms = (time.perf_counter() - started) * 1000.0 if started is not None else 0.0
            detail = str(getattr(exception_context, "original_exception", "")).lower()
            runtime_metrics.observe_db_query(
                duration_ms,
                failed=True,
                busy="locked" in detail or "busy" in detail,
            )
        except Exception:
            pass


def get_engine():
    global _engine
    if _engine is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(
            f"sqlite+aiosqlite:///{DB_FILE}",
            echo=False,
            connect_args={"timeout": DB_BUSY_TIMEOUT_MS / 1000.0},
            pool_pre_ping=True,
        )
        _configure_engine(_engine)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _async_session
    if _async_session is None:
        _async_session = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _async_session


async def get_db() -> AsyncSession:
    """Get an async DB session. Use as: async with await get_db() as db:"""
    factory = get_session_factory()
    return factory()


async def database_ready() -> bool:
    """Return whether the database can service a trivial read right now."""
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def database_file_stats() -> dict[str, int]:
    """Expose bounded local SQLite storage sizes without reading database content."""

    def size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    return {
        "database_bytes": size(DB_FILE),
        "wal_bytes": size(Path(f"{DB_FILE}-wal")),
        "shm_bytes": size(Path(f"{DB_FILE}-shm")),
    }


async def init_db():
    """Create tables, tune WAL mode, and run Alembic migrations."""
    async with get_engine().begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.exec_driver_sql(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")

    # Alembic is synchronous. Keep its startup work off the event-loop thread so
    # startup housekeeping cannot stall other lifespan tasks added in the future.
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{DB_FILE}")
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")

    # Seed DB config from config.toml (file is source of truth on startup)
    await _seed_config_from_toml()

    # Transparently promote historical JSON-stored API keys into the indexed
    # authentication table after migrations have created it.
    from cptr.services.api_keys import migrate_legacy_api_keys

    await migrate_legacy_api_keys()


async def _seed_config_from_toml():
    """Load [app_config] from config.toml and upsert into the DB config table."""
    import logging

    logger = logging.getLogger(__name__)

    try:
        from cptr.utils.config import load_app_config_from_toml

        app_config = load_app_config_from_toml()
        if not app_config:
            return

        from cptr.models.config import Config as ConfigModel

        async with get_session_factory()() as db:
            for key, value in app_config.items():
                existing = await db.get(ConfigModel, key)
                if existing:
                    existing.value = value
                else:
                    db.add(ConfigModel(key=key, value=value))
            await db.commit()

        logger.info("Loaded %d config key(s) from config.toml", len(app_config))
    except Exception:
        logger.warning("Failed to seed config from config.toml", exc_info=True)
