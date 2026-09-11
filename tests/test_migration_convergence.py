import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from cptr.models import Base


class MigrationConvergenceTests(unittest.TestCase):
    @staticmethod
    def _config(path: Path) -> Config:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "cptr" / "migrations")
        )
        config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
        return config

    @staticmethod
    def _version(path: Path) -> str:
        engine = create_engine(f"sqlite:///{path}")
        try:
            with engine.connect() as connection:
                return str(
                    connection.execute(text("select version_num from alembic_version")).scalar_one()
                )
        finally:
            engine.dispose()

    @staticmethod
    def _tables(path: Path) -> set[str]:
        engine = create_engine(f"sqlite:///{path}")
        try:
            with engine.connect() as connection:
                return {
                    str(row[0])
                    for row in connection.execute(
                        text("select name from sqlite_master where type='table'")
                    )
                }
        finally:
            engine.dispose()

    @staticmethod
    def _columns(path: Path, table: str) -> set[str]:
        engine = create_engine(f"sqlite:///{path}")
        try:
            with engine.connect() as connection:
                return {
                    str(row[1]) for row in connection.execute(text(f"PRAGMA table_info({table})"))
                }
        finally:
            engine.dispose()

    def test_merged_history_has_one_head_and_supports_fresh_and_both_legacy_lineages(self):
        script = ScriptDirectory.from_config(self._config(Path("unused.db")))
        self.assertEqual(script.get_heads(), ["0045"])

        browser_tables = [
            Base.metadata.tables[name]
            for name in (
                "browser_devices",
                "browser_pairing_challenges",
                "browser_sessions",
                "browser_leases",
                "browser_device_events",
            )
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            fresh = root / "fresh.db"
            command.upgrade(self._config(fresh), "head")
            self.assertEqual(self._version(fresh), "0045")
            self.assertTrue(
                {
                    "factory_runs",
                    "browser_devices",
                    "memory_fabric_events",
                    "memory_records",
                    "memory_lexical_documents",
                    "memory_retrieval_profiles",
                    "memory_conflicts",
                    "memory_procedure_profiles",
                    "memory_failure_profiles",
                    "capability_os_artifacts",
                    "capability_os_leases",
                    "capability_os_evidence",
                    "capability_os_mcp_mounts",
                    "local_root_grants",
                    "capability_os_mcp_oauth_flows",
                    "capability_os_mcp_oauth_credentials",
                    "capability_os_runs",
                    "capability_os_run_steps",
                    "capability_os_experiments",
                    "capability_os_experiment_runs",
                    "capability_os_promotions",
                    "capability_os_lineage_edges",
                    "capability_os_policy_decisions",
                    "capability_os_observations",
                    "capability_os_mcp_discovery_entries",
                    "capability_os_retention_jobs",
                    "guard_settings",
                    "guard_setting_events",
                    "workspace_groups",
                    "workspace_group_members",
                    "environment_profiles",
                    "environment_profile_versions",
                    "workspace_instruction_versions",
                    "workspace_tasks",
                    "workspace_task_repository_pins",
                    "workspace_task_worker_links",
                    "workspace_task_evidence",
                }
                <= self._tables(fresh)
            )
            self.assertIn("source_memory_ids", self._columns(fresh, "memory_entities"))
            self.assertIn("target_name", self._columns(fresh, "environment_profiles"))
            self.assertIn("head_revision", self._columns(fresh, "workspace_task_repository_pins"))
            self.assertTrue(
                {
                    "environment_profile_id",
                    "environment_profile_override",
                    "admin_role",
                    "role_context",
                    "last_context_snapshot_id",
                }
                <= self._columns(fresh, "workbench_sessions")
            )
            self.assertTrue(
                {"observed_at_ms", "superseded_at_ms"} <= self._columns(fresh, "memory_records")
            )
            self.assertTrue(
                {"sequence", "previous_digest"} <= self._columns(fresh, "capability_os_evidence")
            )
            command.downgrade(self._config(fresh), "0033")
            self.assertEqual(self._version(fresh), "0033")
            self.assertNotIn("capability_os_mcp_oauth_flows", self._tables(fresh))
            self.assertNotIn("capability_os_mcp_oauth_credentials", self._tables(fresh))
            self.assertNotIn("capability_os_runs", self._tables(fresh))
            self.assertNotIn("capability_os_mcp_discovery_entries", self._tables(fresh))
            command.upgrade(self._config(fresh), "head")
            self.assertEqual(self._version(fresh), "0045")
            self.assertTrue(
                {
                    "capability_os_mcp_oauth_flows",
                    "capability_os_mcp_oauth_credentials",
                }
                <= self._tables(fresh)
            )

            factory = root / "factory.db"
            command.upgrade(self._config(factory), "0025")
            self.assertNotIn("browser_devices", self._tables(factory))
            command.upgrade(self._config(factory), "head")
            self.assertEqual(self._version(factory), "0045")
            self.assertTrue(
                {
                    "browser_devices",
                    "memory_fabric_events",
                    "memory_records",
                    "capability_os_artifacts",
                    "capability_os_leases",
                    "local_root_grants",
                    "capability_os_mcp_oauth_flows",
                    "capability_os_mcp_oauth_credentials",
                }
                <= self._tables(factory)
            )

            legacy_main = root / "legacy-main.db"
            command.upgrade(self._config(legacy_main), "0018")
            engine = create_engine(f"sqlite:///{legacy_main}")
            try:
                Base.metadata.create_all(engine, tables=browser_tables, checkfirst=True)
                with engine.begin() as connection:
                    connection.execute(text("update alembic_version set version_num='0019'"))
            finally:
                engine.dispose()
            self.assertNotIn("factory_runs", self._tables(legacy_main))
            self.assertIn("browser_devices", self._tables(legacy_main))

            command.upgrade(self._config(legacy_main), "head")
            self.assertEqual(self._version(legacy_main), "0045")
            self.assertTrue(
                {
                    "factory_runs",
                    "factory_reasoning_calls",
                    "factory_metric_projections",
                    "browser_devices",
                    "memory_fabric_events",
                    "memory_records",
                    "memory_checkpoints",
                    "memory_lexical_documents",
                    "memory_conflicts",
                    "memory_procedure_profiles",
                    "capability_os_artifacts",
                    "capability_os_leases",
                    "capability_os_evidence",
                    "capability_os_mcp_mounts",
                    "local_root_grants",
                    "capability_os_mcp_oauth_flows",
                    "capability_os_mcp_oauth_credentials",
                }
                <= self._tables(legacy_main)
            )


if __name__ == "__main__":
    unittest.main()
