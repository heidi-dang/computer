import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.routers.coding import (
    LspStartRequest,
    WorkerTargetRequest,
    discover_workspace_lsp,
    start_workspace_lsp,
)
from cptr.routers.terminal_extended import ExecRequest, exec_command
from cptr.utils.identity import ExecutionIdentity, env_for
from cptr.utils.terminal import SessionManager


class ExecutionEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.identity = ExecutionIdentity(
            app_user_id="user-1",
            username="runner",
            uid=None,
            gid=None,
            groups=(),
            home="/home/runner",
            shell="/bin/sh",
            is_pam=False,
        )

    def test_parent_secret_is_not_inherited_by_default(self):
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin:/bin",
                "TERM": "xterm",
                "CPTR_SENTINEL_SECRET": "must-not-leak",
                "OPENAI_API_KEY": "must-not-leak-either",
            },
            clear=True,
        ):
            child = env_for(self.identity, Path("/workspace"))

        self.assertNotIn("CPTR_SENTINEL_SECRET", child)
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertEqual(child["PATH"], "/usr/bin:/bin")
        self.assertEqual(child["HOME"], "/home/runner")
        self.assertEqual(child["PWD"], "/workspace")

    def test_capability_must_be_explicitly_granted(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=True):
            child = env_for(
                self.identity,
                "/workspace",
                {"EXPLICIT_TASK_TOKEN": "task-scoped"},
            )
        self.assertEqual(child["EXPLICIT_TASK_TOKEN"], "task-scoped")


class ExecutionEnvironmentCallSiteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.identity = ExecutionIdentity(
            app_user_id="user-1",
            username="runner",
            uid=None,
            gid=None,
            groups=(),
            home="/home/runner",
            shell="/bin/sh",
            is_pam=False,
        )

    def test_pty_session_builder_does_not_copy_parent_secret(self):
        manager = SessionManager()
        fake_session = SimpleNamespace(session_id="session-1")
        with (
            patch.dict(
                os.environ,
                {
                    "PATH": "/usr/bin:/bin",
                    "SHELL": "/bin/sh",
                    "CPTR_SENTINEL_SECRET": "must-not-leak",
                },
                clear=True,
            ),
            patch("cptr.utils.terminal._create_unix", return_value=fake_session) as create_unix,
        ):
            manager.create(self.identity, cwd="/tmp")
        child_env = create_unix.call_args.args[4]
        self.assertNotIn("CPTR_SENTINEL_SECRET", child_env)
        self.assertEqual(child_env["PATH"], "/usr/bin:/bin")

    async def test_extended_exec_does_not_copy_parent_secret(self):
        class FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"ok\n", b""

            def kill(self):
                return None

        create_process = AsyncMock(return_value=FakeProcess())
        request = SimpleNamespace()
        with (
            patch.dict(
                os.environ,
                {
                    "PATH": "/usr/bin:/bin",
                    "CPTR_SENTINEL_SECRET": "must-not-leak",
                },
                clear=True,
            ),
            patch("cptr.routers.terminal_extended._get_user", return_value="user-1"),
            patch(
                "cptr.routers.terminal_extended.identity_for_user_id",
                new=AsyncMock(return_value=self.identity),
            ),
            patch(
                "cptr.routers.terminal_extended.asyncio.create_subprocess_shell",
                new=create_process,
            ),
        ):
            result = await exec_command(request, ExecRequest(command="printf ok"))
        child_env = create_process.await_args.kwargs["env"]
        self.assertNotIn("CPTR_SENTINEL_SECRET", child_env)
        self.assertEqual(result["exit_code"], 0)

    async def test_lsp_discovery_uses_same_sanitized_workspace_environment_as_start(self):
        request = SimpleNamespace(state=SimpleNamespace(control_scopes=set()))
        discover = unittest.mock.Mock(return_value={"servers": []})
        with tempfile.TemporaryDirectory() as workspace_root:
            workspace = SimpleNamespace(path=workspace_root, user_id="user-1")
            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin:/bin",
                        "CPTR_SENTINEL_SECRET": "must-not-leak",
                    },
                    clear=True,
                ),
                patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
                patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
                patch(
                    "cptr.routers.coding._coding_root",
                    new=AsyncMock(return_value=Path(workspace_root)),
                ),
                patch(
                    "cptr.routers.coding.identity_for_context",
                    new=AsyncMock(return_value=self.identity),
                ),
                patch("cptr.routers.coding.lsp_manager.discover", new=discover),
            ):
                await discover_workspace_lsp(
                    request,
                    "workspace-1",
                    WorkerTargetRequest(),
                )
        child_env = discover.call_args.kwargs["env"]
        self.assertNotIn("CPTR_SENTINEL_SECRET", child_env)
        self.assertEqual(discover.call_args.kwargs["root"], Path(workspace_root))
        self.assertEqual(child_env["PATH"], "/usr/bin:/bin")

    async def test_lsp_start_does_not_copy_parent_secret(self):
        request = SimpleNamespace(state=SimpleNamespace(control_scopes=set()))
        start = AsyncMock(return_value={"lsp_id": "lsp-1"})
        with tempfile.TemporaryDirectory() as workspace_root:
            workspace = SimpleNamespace(path=workspace_root, user_id="user-1")
            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin:/bin",
                        "CPTR_SENTINEL_SECRET": "must-not-leak",
                    },
                    clear=True,
                ),
                patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
                patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
                patch(
                    "cptr.routers.coding._coding_root",
                    new=AsyncMock(return_value=Path(workspace_root)),
                ),
                patch(
                    "cptr.routers.coding.identity_for_context",
                    new=AsyncMock(return_value=self.identity),
                ),
                patch("cptr.routers.coding.lsp_manager.start", new=start),
            ):
                await start_workspace_lsp(
                    request,
                    "workspace-1",
                    LspStartRequest(server_id="pyright", root="."),
                )
        child_env = start.await_args.kwargs["env"]
        self.assertNotIn("CPTR_SENTINEL_SECRET", child_env)
        self.assertEqual(child_env["PATH"], "/usr/bin:/bin")


if __name__ == "__main__":
    unittest.main()
