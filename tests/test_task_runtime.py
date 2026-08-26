import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.utils.agents.attachments import prepare_agent_attachments
from cptr.utils.tools import run_command
from cptr.utils.task_runtime import (
    ensure_task_runtime,
    task_runtime_dir,
    task_runtime_environment,
)


class TaskRuntimeLayoutTests(unittest.TestCase):
    def test_task_runtime_is_stable_and_grouped_under_configured_root(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = ensure_task_runtime("task_abc123", root=root)

            self.assertEqual(runtime, Path(root) / "task_abc123")
            self.assertTrue(runtime.is_dir())
            self.assertEqual(
                sorted(path.name for path in runtime.iterdir()),
                ["agent", "attachments", "browser", "command-output"],
            )
            self.assertEqual(task_runtime_dir("task_abc123", root=root), runtime)

    def test_all_runtime_categories_share_one_task_owned_root(self):
        with tempfile.TemporaryDirectory() as root:
            first = ensure_task_runtime("task_grouped", root=root)
            second = ensure_task_runtime("task_grouped", root=root)

            self.assertEqual(first, second)
            self.assertEqual(first.parent, Path(root).resolve())
            for category in ("agent", "attachments", "browser", "command-output"):
                category_path = first / category
                self.assertTrue(category_path.is_dir())
                self.assertTrue(category_path.is_relative_to(first))
            self.assertEqual(sorted(path.name for path in Path(root).iterdir()), ["task_grouped"])

    def test_task_id_cannot_escape_runtime_root(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                task_runtime_dir("../outside", root=root)
            with self.assertRaises(ValueError):
                task_runtime_dir("task/child", root=root)

    def test_provider_environment_is_scoped_to_task_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            env = task_runtime_environment("task_provider", root=root, base={"HOME": "/user"})
            runtime = Path(root) / "task_provider" / "agent"

            self.assertEqual(env["HOME"], "/user")
            self.assertEqual(Path(env["XDG_CONFIG_HOME"]), runtime / "config")
            self.assertEqual(Path(env["XDG_DATA_HOME"]), runtime / "data")
            self.assertEqual(Path(env["XDG_CACHE_HOME"]), runtime / "cache")
            self.assertTrue(runtime.is_dir())

    def test_attachment_staging_uses_task_runtime_not_workspace(self):
        async def run():
            with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as root:
                storage = AsyncMock()
                storage.get.return_value = b"print('ok')\n"
                with (
                    patch("cptr.utils.agents.attachments.get_storage", return_value=storage),
                    patch(
                        "cptr.utils.agents.attachments.Runtime.write_file", new=AsyncMock()
                    ) as write_file,
                ):
                    prepared = await prepare_agent_attachments(
                        None,
                        workspace=workspace,
                        runtime_root=root,
                        user_id="user-1",
                        chat_id="chat-1",
                        message_id="message-1",
                        files=[{"id": "file-1", "name": "script.py"}],
                    )

                staged_path = Path(prepared.files[0].path)
                self.assertTrue(staged_path.is_relative_to(Path(root)))
                self.assertFalse(staged_path.is_relative_to(Path(workspace)))
                write_file.assert_awaited_once()

        asyncio.run(run())

    def test_command_logs_are_written_to_task_runtime(self):
        async def run():
            with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as root:
                request = SimpleNamespace(state=SimpleNamespace(auth=None))
                with (
                    patch("cptr.utils.tools.TASK_ROOT", Path(root), create=True),
                    patch("cptr.utils.tools.Runtime.write_file", new=AsyncMock()),
                ):
                    result = await run_command(
                        "printf runtime-ok",
                        cwd=".",
                        wait=2,
                        __context__={
                            "workspace": workspace,
                            "request": request,
                            "user_id": "user-1",
                            "task_runtime_id": "task-command",
                        },
                    )

                self.assertIn("exited (code 0)", result)
                logs = list((Path(root) / "task-command" / "command-output").glob("*.jsonl"))
                self.assertEqual(len(logs), 1)
                self.assertIn("runtime-ok", logs[0].read_text(encoding="utf-8"))
                self.assertFalse((Path(workspace) / ".cptr" / "task_logs").exists())

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
