from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

from fastapi import HTTPException

from cptr.routers.coding import (
    TestTargetRequest as CodingTestTargetRequest,
    WorkspaceInspectRequest,
    _workspace_insight,
    run_workspace_test_target,
)


class WorkspaceInsightsTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_inventory_is_read_only_and_bounded(self):
        request = SimpleNamespace()
        body = WorkspaceInspectRequest(kind="project")
        with patch(
            "cptr.routers.coding._known_project_files",
            new=AsyncMock(return_value=["package.json", "pyproject.toml", "Cargo.toml"]),
        ):
            result = await _workspace_insight(
                request,
                root=Path("/tmp/cptr-workspace"),
                body=body,
                user_id="user_1",
            )

        self.assertEqual(result["project_files"], ["package.json", "pyproject.toml", "Cargo.toml"])
        self.assertEqual(result["detected_runtimes"], ["node", "python", "rust"])

    async def test_fixed_python_test_profile_constructs_the_command_server_side(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-workspace")
        body = CodingTestTargetRequest(target="python_pytest", test_path="tests/test_example.py", wait_seconds=5)
        with patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")), patch(
            "cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)
        ), patch("cptr.routers.coding.run_command", new=AsyncMock(return_value="Task deadbeef: started")) as run, patch(
            "cptr.routers.coding._command_snapshot",
            new=AsyncMock(return_value={"command_id": "deadbeef", "status": "RUNNING", "exit_code": None, "output": "", "next_offset": 0}),
        ):
            result = await run_workspace_test_target(request, "ws_1", body)

        self.assertEqual(result["target"], "python_pytest")
        self.assertEqual(run.await_args.args[:3], ("python3 -m pytest tests/test_example.py", ".", 5))
        self.assertEqual(run.await_args.kwargs["__context__"]["workspace_id"], "ws_1")
        self.assertEqual(run.await_args.kwargs["__context__"]["user_id"], "user_1")

    async def test_test_path_with_shell_syntax_is_rejected_before_runner_execution(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-workspace")
        body = CodingTestTargetRequest(target="python_pytest", test_path="tests/test;unsafe.py")
        with patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")), patch(
            "cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)
        ), patch("cptr.routers.coding.run_command", new=AsyncMock()) as run:
            with self.assertRaises(HTTPException) as error:
                await run_workspace_test_target(request, "ws_1", body)

        self.assertEqual(error.exception.status_code, 403)
        run.assert_not_awaited()
