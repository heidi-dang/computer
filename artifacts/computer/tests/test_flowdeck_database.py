import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cptr.flowdeck.database import (
    DatabaseCancelledError,
    DatabaseContractError,
    DatabaseRequest,
    ProjectDatabaseService,
    cancel_database_operation,
    register_database_operation,
    unregister_database_operation,
)


class ProjectDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "project.db"
        with sqlite3.connect(self.db) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE);
                CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                  body TEXT, FOREIGN KEY(user_id) REFERENCES users(id));
                CREATE INDEX posts_user_id ON posts(user_id);
                INSERT INTO users VALUES (1, 'one@example.test');
                INSERT INTO posts VALUES (1, 1, 'hello');
                """
            )
        self.request = DatabaseRequest("db-test-key", "user-1", str(self.root))
        self.service = ProjectDatabaseService()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_schema_query_relationships_and_integrity(self):
        inspected = await self.service.inspect(self.request)
        self.assertEqual(inspected["integrity"], "ok")
        posts = next(table for table in inspected["schema"]["tables"] if table["name"] == "posts")
        self.assertEqual(posts["foreign_keys"][0]["table"], "users")
        self.assertEqual(posts["indexes"][0]["columns"], ["user_id"])
        result = await self.service.query(self.request, "SELECT email FROM users WHERE id = ?", [1])
        self.assertEqual(result["rows"], [{"email": "one@example.test"}])

    async def test_migration_snapshots_and_restore(self):
        migrated = await self.service.migrate(self.request, "ALTER TABLE users ADD COLUMN display_name TEXT")
        self.assertTrue(migrated["verified"])
        self.assertIsNotNone(migrated["snapshot"])
        users = next(table for table in migrated["after"]["schema"]["tables"] if table["name"] == "users")
        self.assertEqual(len(users["columns"]), 3)
        restored = await self.service.restore(self.request, migrated["snapshot"])
        users = next(table for table in restored["schema"]["tables"] if table["name"] == "users")
        self.assertNotIn("display_name", {column["name"] for column in users["columns"]})

    async def test_destructive_and_unsafe_nullable_changes_are_denied(self):
        for sql in ("DROP TABLE users", "ALTER TABLE users ADD COLUMN required TEXT NOT NULL"):
            with self.assertRaises(DatabaseContractError):
                await self.service.migrate(self.request, sql)

    async def test_workspace_boundary_and_concurrent_reads(self):
        with self.assertRaises(DatabaseContractError):
            await self.service.inspect(DatabaseRequest("bad", "user-1", str(self.root), database="../outside.db"))
        results = await asyncio.gather(
            *(self.service.query(self.request, "SELECT id FROM posts") for _ in range(8))
        )
        self.assertEqual([item["rows"][0]["id"] for item in results], [1] * 8)

    async def test_postgresql_never_accepts_request_credentials_or_internal_database(self):
        request = DatabaseRequest("pg", "user-1", str(self.root), engine="postgresql", database="postgres")
        if os.environ.get("CPTR_PROJECT_DATABASE_URL"):
            result = await self.service.inspect(request)
            self.assertEqual(result["schema"]["engine"], "postgresql")
        else:
            with self.assertRaises(DatabaseContractError):
                await self.service.inspect(request)

    async def test_postgresql_fixture_operations_and_transactional_rollback(self):
        project_url = os.environ.get("CPTR_PROJECT_DATABASE_URL")
        if not project_url:
            self.skipTest("isolated project PostgreSQL fixture is not configured")
        import psycopg

        with psycopg.connect(project_url) as connection:
            connection.execute("DROP TABLE IF EXISTS phase9_posts CASCADE")
            connection.execute("DROP TABLE IF EXISTS phase9_users CASCADE")
            connection.execute("CREATE TABLE phase9_users (id integer PRIMARY KEY, email text NOT NULL UNIQUE)")
            connection.execute("CREATE TABLE phase9_posts (id integer PRIMARY KEY, user_id integer NOT NULL REFERENCES phase9_users(id), body text)")
            connection.execute("CREATE INDEX phase9_posts_user_id ON phase9_posts(user_id)")
            connection.execute("INSERT INTO phase9_users VALUES (1, 'fixture@example.test')")
        try:
            request = DatabaseRequest("pg-ops", "user-1", str(self.root), engine="postgresql")
            inspected = await self.service.inspect(request)
            posts = next(item for item in inspected["schema"]["tables"] if item["name"] == "phase9_posts")
            self.assertEqual(posts["foreign_keys"][0]["table"], "phase9_users")
            self.assertTrue(posts["indexes"])
            rows = await asyncio.gather(
                *(self.service.query(request, "SELECT email FROM phase9_users WHERE id = %s", [1]) for _ in range(6))
            )
            self.assertEqual([item["rows"][0]["email"] for item in rows], ["fixture@example.test"] * 6)
            migrated = await self.service.migrate(request, "ALTER TABLE phase9_users ADD COLUMN display_name text")
            self.assertEqual(migrated["snapshot"], "transactional pre-migration state")
            with self.assertRaises(DatabaseContractError):
                await self.service.migrate(request, "ALTER TABLE phase9_users ADD COLUMN broken text; ALTER TABLE missing_table ADD COLUMN x text")
            with psycopg.connect(project_url) as connection:
                self.assertIsNone(connection.execute("SELECT 1 FROM information_schema.columns WHERE table_name='phase9_users' AND column_name='broken'").fetchone())
        finally:
            with psycopg.connect(project_url) as connection:
                connection.execute("DROP TABLE IF EXISTS phase9_posts CASCADE")
                connection.execute("DROP TABLE IF EXISTS phase9_users CASCADE")

    async def test_postgresql_query_and_migration_cancellation_interrupt_and_rollback(self):
        project_url = os.environ.get("CPTR_PROJECT_DATABASE_URL")
        if not project_url:
            self.skipTest("isolated project PostgreSQL fixture is not configured")
        import psycopg

        request = DatabaseRequest("pg-cancel", "user-1", str(self.root), engine="postgresql")
        with psycopg.connect(project_url) as connection:
            connection.execute("DROP TABLE IF EXISTS phase9_cancel CASCADE")
            connection.execute("CREATE TABLE phase9_cancel (id integer PRIMARY KEY)")
        try:
            query_handle = register_database_operation("pg-cancel-query")
            query_task = asyncio.create_task(
                self.service.query(request, "SELECT pg_sleep(30)", handle=query_handle)
            )
            await asyncio.sleep(0.2)
            self.assertTrue(cancel_database_operation("pg-cancel-query"))
            with self.assertRaises(DatabaseCancelledError):
                await query_task

            migration_handle = register_database_operation("pg-cancel-migration")
            migration_task = asyncio.create_task(
                self.service.migrate(
                    request,
                    "SELECT pg_sleep(30); ALTER TABLE phase9_cancel ADD COLUMN should_not_exist text",
                    handle=migration_handle,
                )
            )
            await asyncio.sleep(0.2)
            migration_handle.cancel()
            with self.assertRaises(DatabaseCancelledError):
                await migration_task
            with psycopg.connect(project_url) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM information_schema.columns WHERE table_name='phase9_cancel' AND column_name='should_not_exist'"
                    ).fetchone()
                )
        finally:
            unregister_database_operation("pg-cancel-query")
            unregister_database_operation("pg-cancel-migration")
            with psycopg.connect(project_url) as connection:
                connection.execute("DROP TABLE IF EXISTS phase9_cancel CASCADE")
