import os
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


class FlowDeckMigrationTests(unittest.TestCase):
    def setUp(self):
        with NamedTemporaryFile(suffix=".db", delete=False) as db_file:
            self.db_path = db_file.name

        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.alembic = Config()
        self.alembic.set_main_option(
            "script_location",
            str(Path(__file__).parents[1] / "cptr" / "migrations"),
        )
        self.alembic.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path}")

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)

    def test_existing_flowdeck_state_survives_0007_and_0008(self):
        command.upgrade(self.alembic, "0006")

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO flowdeck_runs
                        (id, request_key, workspace, owner, status, heartbeat_at,
                         created_at, updated_at, version)
                    VALUES
                        ('run-1', 'request-1', '/workspace', 'owner-1', 'RUNNING',
                         1005, 1000, 1005, 1)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO flowdeck_steps
                        (id, run_id, sequence, name, status, created_at, updated_at)
                    VALUES
                        ('step-1', 'run-1', 1, 'Inspect workspace', 'SUCCEEDED', 1001, 1002)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO flowdeck_logical_operations
                        (id, run_id, step_id, idempotency_key, capability, target,
                         reconcile_kind, status, intent_at, updated_at, outcome,
                         authoritative_evidence)
                    VALUES
                        ('operation-1', 'run-1', 'step-1', 'operation-key-1', 'write_files',
                         'workspace.txt', 'file_hash', 'SUCCEEDED', 1002, 1004, 'succeeded',
                         '{"source": "verifier", "hash": "abc123"}')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO flowdeck_physical_attempts
                        (id, operation_id, attempt_no, status, fencing_epoch, started_at,
                         heartbeat_at, ended_at, outcome, error)
                    VALUES
                        ('attempt-1', 'operation-1', 1, 'SUCCEEDED', 1, 1002, 1003, 1004,
                         'succeeded', NULL)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO flowdeck_events
                        (id, run_id, sequence, kind, payload, created_at)
                    VALUES
                        ('event-1', 'run-1', 1, 'verification', '{"hash": "abc123"}', 1004)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO flowdeck_workspace_leases
                        (workspace, run_id, owner, epoch, acquired_at, heartbeat_at, expires_at)
                    VALUES
                        ('/workspace', 'run-1', 'owner-1', 1, 1000, 1005, 2000)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO flowdeck_recovery_leases
                        (run_id, owner, epoch, acquired_at, heartbeat_at, expires_at)
                    VALUES
                        ('run-1', 'recovery-1', 2, 1006, 1007, 2001)
                    """
                )
            )

        command.upgrade(self.alembic, "head")

        with self.engine.connect() as connection:
            run = connection.execute(
                text(
                    "SELECT id, request_key, status, version FROM flowdeck_runs WHERE id = 'run-1'"
                )
            ).one()
            self.assertEqual(run, ("run-1", "request-1", "RUNNING", 1))

            workspace_lease = connection.execute(
                text(
                    """
                    SELECT workspace, run_id, owner, epoch, expires_at
                    FROM flowdeck_workspace_leases
                    WHERE workspace = '/workspace'
                    """
                )
            ).one()
            self.assertEqual(workspace_lease, ("/workspace", "run-1", "owner-1", 1, 2000))

            recovery_lease = connection.execute(
                text(
                    """
                    SELECT run_id, owner, epoch, expires_at
                    FROM flowdeck_recovery_leases
                    WHERE run_id = 'run-1'
                    """
                )
            ).one()
            self.assertEqual(recovery_lease, ("run-1", "recovery-1", 2, 2001))

            attempt = connection.execute(
                text(
                    """
                    SELECT id, operation_id, attempt_no, status, fencing_epoch, outcome
                    FROM flowdeck_physical_attempts
                    WHERE id = 'attempt-1'
                    """
                )
            ).one()
            self.assertEqual(
                attempt,
                ("attempt-1", "operation-1", 1, "SUCCEEDED", 1, "succeeded"),
            )

            evidence = connection.execute(
                text(
                    """
                    SELECT payload
                    FROM flowdeck_events
                    WHERE id = 'event-1'
                    """
                )
            ).scalar_one()
            self.assertEqual(evidence, '{"hash": "abc123"}')

            operation_evidence = connection.execute(
                text(
                    """
                    SELECT authoritative_evidence
                    FROM flowdeck_logical_operations
                    WHERE id = 'operation-1'
                    """
                )
            ).scalar_one()
            self.assertEqual(operation_evidence, '{"source": "verifier", "hash": "abc123"}')

            scope_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(autonomous_scopes)"))
            }
            monitor_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(autonomous_monitors)"))
            }
            self.assertIn("failure_signature_counts", scope_columns)
            self.assertIn("approved_operations", monitor_columns)
            self.assertEqual(
                connection.execute(
                    text("SELECT failure_signature_counts FROM autonomous_scopes")
                ).fetchall(),
                [],
            )
            self.assertEqual(
                connection.execute(
                    text("SELECT approved_operations FROM autonomous_monitors")
                ).fetchall(),
                [],
            )
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE name = 'autonomous_workspace_leases'"
                    )
                ).scalar_one(),
                "autonomous_workspace_leases",
            )
