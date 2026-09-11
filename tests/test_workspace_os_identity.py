import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


class WorkspaceOsIdentityMigrationTests(unittest.TestCase):
    @staticmethod
    def _config(path: Path) -> Config:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "cptr" / "migrations")
        )
        config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
        return config

    def test_identity_migration_backfills_unique_slugs_and_preserves_checkouts(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "workspace-os.db"
            cfg = self._config(db_path)
            command.upgrade(cfg, "0036")

            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO users "
                            "(id, display_name, profile_image_url, role, settings, created_at, "
                            "updated_at, last_seen_at) "
                            "VALUES ('u1', NULL, NULL, 'admin', '{}', 1, NULL, NULL)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO workspaces "
                            "(id, user_id, path, name, data, created_at, updated_at) VALUES "
                            "('ws-a', 'u1', '/repo/a', 'ChatGPT Computer Plugin', '{}', 1, 1), "
                            "('ws-b', 'u1', '/repo/b', 'ChatGPT Computer Plugin', '{}', 2, 2)"
                        )
                    )
            finally:
                engine.dispose()

            command.upgrade(cfg, "head")
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.begin() as connection:
                    slugs = list(
                        connection.execute(
                            text("SELECT slug FROM workspaces ORDER BY created_at")
                        ).scalars()
                    )
                    self.assertEqual(slugs[0], "chatgpt-computer-plugin")
                    self.assertNotEqual(slugs[0], slugs[1])
                    self.assertTrue(slugs[1].startswith("chatgpt-computer-plugin-"))

                    tables = {
                        row[0]
                        for row in connection.execute(
                            text("SELECT name FROM sqlite_master WHERE type='table'")
                        )
                    }
                    self.assertTrue(
                        {
                            "repositories",
                            "repository_checkouts",
                            "workspace_repositories",
                            "workspace_aliases",
                        }
                        <= tables
                    )

                    connection.execute(
                        text(
                            "INSERT INTO repositories "
                            "(id,user_id,name,canonical_remote_identity,provider,default_branch,"
                            "remote_metadata,created_at,updated_at) "
                            "VALUES ('repo-1','u1','plugin','github.com/heidi-dang/plugin',"
                            "'github','main','{}',1,1)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO repository_checkouts "
                            "(id,repository_id,path,checkout_kind,canonical,branch,upstream,"
                            "git_worktree_id,last_seen_revision,available,created_at,last_seen_at) "
                            "VALUES "
                            "('co-1','repo-1','/repo/a','canonical',1,'main','origin/main',"
                            "NULL,'sha-a',1,1,1),"
                            "('co-2','repo-1','/repo/b','secondary',0,'feature','origin/feature',"
                            "NULL,'sha-b',1,1,1)"
                        )
                    )
                    count = connection.execute(
                        text(
                            "SELECT count(*) FROM repository_checkouts "
                            "WHERE repository_id='repo-1'"
                        )
                    ).scalar_one()
                    self.assertEqual(count, 2)
            finally:
                engine.dispose()

            command.downgrade(cfg, "0036")
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as connection:
                    columns = {
                        row[1] for row in connection.execute(text("PRAGMA table_info(workspaces)"))
                    }
                    self.assertNotIn("slug", columns)
                    self.assertNotIn("workspace_type", columns)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
