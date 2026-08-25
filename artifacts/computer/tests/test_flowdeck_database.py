import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cptr.flowdeck.database import (
    DatabaseContractError,
    DatabaseRequest,
    ProjectDatabaseService,
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
        with self.assertRaises(DatabaseContractError):
            await self.service.inspect(request)
