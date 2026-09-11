import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


class WorkspaceChatIdentityMigrationTests(unittest.TestCase):
    @staticmethod
    def _config(path: Path) -> Config:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "cptr" / "migrations")
        )
        config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
        return config

    def test_backfills_unambiguous_legacy_path_and_preserves_meta(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "chat-workspace.db"
            cfg = self._config(db_path)
            command.upgrade(cfg, "0037")
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO users "
                            "(id,display_name,profile_image_url,role,settings,created_at,"
                            "updated_at,last_seen_at) "
                            "VALUES ('u1',NULL,NULL,'admin','{}',1,NULL,NULL)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO workspaces "
                            "(id,user_id,path,name,slug,workspace_type,data,created_at,updated_at) "
                            "VALUES ('ws1','u1','/repo/a','A','a','project','{}',1,1)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO chats "
                            "(id,user_id,title,summary,current_message_id,meta,created_at,"
                            "updated_at,last_read_at) "
                            "VALUES ('chat1','u1','Legacy',NULL,NULL,:meta,1,1,NULL)"
                        ),
                        {"meta": '{"workspace":"/repo/a"}'},
                    )
            finally:
                engine.dispose()

            command.upgrade(cfg, "head")
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as connection:
                    row = connection.execute(
                        text("SELECT workspace_id, meta FROM chats WHERE id='chat1'")
                    ).one()
                    self.assertEqual(row.workspace_id, "ws1")
                    self.assertIn("/repo/a", row.meta)
            finally:
                engine.dispose()

    def test_ambiguous_legacy_path_is_not_backfilled(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "chat-ambiguous.db"
            cfg = self._config(db_path)
            command.upgrade(cfg, "0037")
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO users "
                            "(id,display_name,profile_image_url,role,settings,created_at,"
                            "updated_at,last_seen_at) "
                            "VALUES ('u1',NULL,NULL,'admin','{}',1,NULL,NULL)"
                        )
                    )
                    # Existing schema enforces unique owner+path, so ambiguity is
                    # represented by a path with no matching registered workspace.
                    connection.execute(
                        text(
                            "INSERT INTO chats "
                            "(id,user_id,title,summary,current_message_id,meta,created_at,"
                            "updated_at,last_read_at) "
                            "VALUES ('chat1','u1','Legacy',NULL,NULL,:meta,1,1,NULL)"
                        ),
                        {"meta": '{"workspace":"/missing"}'},
                    )
            finally:
                engine.dispose()
            command.upgrade(cfg, "head")
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as connection:
                    value = connection.execute(
                        text("SELECT workspace_id FROM chats WHERE id='chat1'")
                    ).scalar_one()
                    self.assertIsNone(value)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
