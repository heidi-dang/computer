import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

SEED_SCRIPT = r"""
import asyncio
import os
from cptr.models import Chat, ChatMessage, ControlTask, User, Workspace
from cptr.services.control_store import SqlSupervisorStore
from cptr.services.supervisor import MonitorState, ScopeRecord, ScopeStatus
from cptr.utils.db import get_db, init_db

async def main():
    await init_db()
    user_id = await User.create("restart-test", "password-hash", role="user", created_at=1)
    workspace = await Workspace.upsert(user_id, os.environ["RESTART_WORKSPACE"], "restart-test", {})
    chat = await Chat.create(user_id=user_id, title="restart task", meta={"workspace": workspace.path, "internal": True, "control_plane": True}, created_at=1)
    user_message = await ChatMessage.create(chat_id=chat.id, role="user", content="durable work", created_at=1)
    assistant_message = await ChatMessage.create(chat_id=chat.id, role="assistant", content="durable output", parent_id=user_message.id, model="unconfigured-model", done=True, created_at=2)
    await Chat.update_current_message(chat.id, assistant_message.id, 2)
    task_id = "task_restart_existing"
    monitor_id = "mon_restart_existing"
    scope_id = "scope_restart_existing"
    task_key = f"{monitor_id}:{scope_id}:1"
    async with await get_db() as db:
        db.add(ControlTask(id=task_id, user_id=user_id, workspace_id=workspace.id, chat_id=chat.id, message_id=assistant_message.id, status="RUNNING", prompt="durable work", model_id="unconfigured-model", idempotency_key=task_key, created_at=1, updated_at=1))
        await db.commit()
    monitor = MonitorState(monitor_id=monitor_id, goal_id="goal_restart_existing", user_id=user_id, workspace_id=workspace.id, original_goal="Recover the existing task", original_acceptance_criteria=["The existing task is reused"], model_id="unconfigured-model", scopes=[ScopeRecord(scope_id=scope_id, title="The existing task is reused", description="Recover the existing task: The existing task is reused", acceptance_criteria=["The existing task is reused"], status=ScopeStatus.PENDING)])
    await SqlSupervisorStore().create_monitor(monitor, "restart-monitor-key")

asyncio.run(main())
"""


class RestartRecoveryTests(unittest.TestCase):
    def test_active_monitor_recovers_without_duplicate_worker_task(self):
        with (
            tempfile.TemporaryDirectory() as data_dir,
            tempfile.TemporaryDirectory() as workspace_dir,
        ):
            subprocess.run(["git", "init", "-q", workspace_dir], check=True)
            env = {**os.environ, "CPTR_DATA_DIR": data_dir, "RESTART_WORKSPACE": workspace_dir}
            subprocess.run([sys.executable, "-c", SEED_SCRIPT], check=True, env=env)
            port = self._free_port()
            first = self._start_server(data_dir, port, poll_interval="30")
            second = None
            try:
                self._wait_for_health(port)
                time.sleep(0.5)
                self.assertEqual(
                    self._query(data_dir, "select status from autonomous_monitors"), "RUNNING"
                )
                first.terminate()
                first.wait(timeout=10)

                second = self._start_server(data_dir, port, poll_interval="0.05")
                self._wait_for_health(port)
                self._wait_until(
                    lambda: (
                        self._query(data_dir, "select status from autonomous_monitors")
                        == "COMPLETE"
                    )
                )
                self.assertEqual(self._query(data_dir, "select count(*) from control_tasks"), "1")
            finally:
                for process in (first, second):
                    if process and process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)

    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @staticmethod
    def _start_server(data_dir, port, poll_interval):
        env = {
            **os.environ,
            "CPTR_DATA_DIR": data_dir,
            "CPTR_SUPERVISOR_POLL_INTERVAL": poll_interval,
            "CPTR_SUPERVISOR_MAX_ATTEMPTS": "3",
            "CPTR_CONTROL_PLANE_ENABLED": "true",
        }
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cptr.cli",
                "run",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--headless",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _wait_for_health(port):
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=1
                ) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("CPTR health endpoint did not start")

    @staticmethod
    def _query(data_dir, query):
        import sqlite3

        with sqlite3.connect(os.path.join(data_dir, "app.db")) as connection:
            return str(connection.execute(query).fetchone()[0])

    @staticmethod
    def _wait_until(predicate):
        deadline = time.time() + 15
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        raise AssertionError("condition did not become true before timeout")


if __name__ == "__main__":
    unittest.main()
