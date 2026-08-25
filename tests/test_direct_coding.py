import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from cptr.app import app as cptr_app
from cptr.app import application as cptr_application
from cptr.routers.coding import (
    CommandRequest,
    EditRequest,
    _relative_path,
    _validate_command,
    edit_workspace_file,
    start_workspace_command,
)
from cptr.routers.coding import router as coding_router
from cptr.routers.gateway import CreateApiKeyRequest, create_api_key


class DirectCodingAppRegistrationTests(unittest.TestCase):
    def test_production_application_dispatches_a_direct_coding_route(self):
        token = "production-route-token"
        key = {
            "key_hash": hashlib.sha256(token.encode()).hexdigest(),
            "user_id": "user_1",
            "scopes": ["coding:read"],
        }
        headers = {"Authorization": f"Bearer {token}"}
        with tempfile.TemporaryDirectory() as workspace_root:
            Path(workspace_root, "example.py").write_text("value = 1\n", encoding="utf-8")
            workspace = SimpleNamespace(path=workspace_root, user_id="user_1")
            with (
                patch(
                    "cptr.services.control_auth._get_api_keys",
                    new=AsyncMock(return_value=[key]),
                ),
                patch(
                    "cptr.services.control_auth.Auth.get_by_user_id",
                    new=AsyncMock(return_value=SimpleNamespace(username="tester")),
                ),
                patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            ):
                client = TestClient(cptr_app)
                response = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/read",
                    headers=headers,
                    json={"path": "example.py"},
                )
                client.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "value = 1\n")

    def test_socketio_wrapped_production_asgi_dispatches_direct_coding(self):
        token = "production-asgi-route-token"
        key = {
            "key_hash": hashlib.sha256(token.encode()).hexdigest(),
            "user_id": "user_1",
            "scopes": ["coding:read"],
        }
        headers = {"Authorization": f"Bearer {token}"}
        with tempfile.TemporaryDirectory() as workspace_root:
            Path(workspace_root, "wrapped.py").write_text("wrapped = True\n", encoding="utf-8")
            workspace = SimpleNamespace(path=workspace_root, user_id="user_1")
            with (
                patch(
                    "cptr.services.control_auth._get_api_keys",
                    new=AsyncMock(return_value=[key]),
                ),
                patch(
                    "cptr.services.control_auth.Auth.get_by_user_id",
                    new=AsyncMock(return_value=SimpleNamespace(username="tester")),
                ),
                patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
                TestClient(cptr_application) as client,
            ):
                response = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/read",
                    headers=headers,
                    json={"path": "wrapped.py"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "wrapped = True\n")


class DirectCodingApiTests(unittest.IsolatedAsyncioTestCase):
    def test_relative_path_is_confined_to_workspace_and_hides_environment_files(self):
        root = Path("/tmp/cptr-direct-coding").resolve()
        full, relative = _relative_path("src/main.py", root)
        self.assertEqual(full, root / "src/main.py")
        self.assertEqual(relative, "src/main.py")

        for unsafe in ("../outside.py", "/etc/passwd", ".env", "config/.env.local"):
            with self.subTest(path=unsafe), self.assertRaises(HTTPException):
                _relative_path(unsafe, root)

    def test_command_policy_rejects_destructive_and_unapproved_network_commands(self):
        with self.assertRaises(HTTPException) as destructive:
            _validate_command("rm -rf build", False)
        self.assertEqual(destructive.exception.status_code, 403)

        with self.assertRaises(HTTPException) as network:
            _validate_command("npm install example-package", False)
        self.assertEqual(network.exception.status_code, 403)

        _validate_command("npm install example-package", True)
        _validate_command("npm test", False)

    async def test_exact_edit_uses_authorized_workspace_and_never_starts_an_agent(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = EditRequest(
            path="src/app.py",
            target="return 'old'",
            replacement="return 'new'",
        )
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.Runtime.read_file",
                new=AsyncMock(return_value={"binary": False, "content": "def f():\n    return 'old'\n"}),
            ) as read_file,
            patch("cptr.routers.coding.Runtime.write_file", new=AsyncMock(return_value={})) as write_file,
        ):
            result = await edit_workspace_file(request, "ws_1", body)

        self.assertEqual(result["path"], "src/app.py")
        read_file.assert_awaited_once()
        write_file.assert_awaited_once_with(
            request,
            "/tmp/cptr-direct-coding/src/app.py",
            "def f():\n    return 'new'\n",
        )

    async def test_direct_command_uses_no_model_or_agent_inputs(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = CommandRequest(command="npm test", cwd=".", wait_seconds=5)
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.run_command", new=AsyncMock(return_value="Task deadbeef: exited (code 0)")) as run,
            patch(
                "cptr.routers.coding._command_snapshot",
                new=AsyncMock(
                    return_value={
                        "command_id": "deadbeef",
                        "status": "COMPLETE",
                        "exit_code": 0,
                        "output": "tests pass",
                        "next_offset": 10,
                    }
                ),
            ),
        ):
            result = await start_workspace_command(request, "ws_1", body)

        self.assertEqual(result["status"], "COMPLETE")
        run.assert_awaited_once_with(
            "npm test",
            ".",
            5,
            __context__={
                "workspace": "/tmp/cptr-direct-coding",
                "request": request,
                "user_id": "user_1",
            },
        )


class DirectCodingHttpFlowTests(unittest.TestCase):
    def test_scoped_http_routes_write_read_and_run_without_an_agent(self):
        app = FastAPI()
        app.include_router(coding_router)

        token = "direct-coding-token"
        key = {
            "key_hash": hashlib.sha256(token.encode()).hexdigest(),
            "user_id": "user_1",
            "scopes": ["coding:read", "coding:write", "command:execute"],
        }
        headers = {"Authorization": f"Bearer {token}"}

        with tempfile.TemporaryDirectory() as workspace_root:
            workspace = SimpleNamespace(path=workspace_root, user_id="user_1")
            with (
                patch(
                    "cptr.services.control_auth._get_api_keys",
                    new=AsyncMock(return_value=[key]),
                ),
                patch(
                    "cptr.services.control_auth.Auth.get_by_user_id",
                    new=AsyncMock(return_value=SimpleNamespace(username="tester")),
                ),
                patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
                TestClient(app) as client,
            ):
                write = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/write",
                    headers=headers,
                    json={"path": "src/example.py", "content": "value = 1\n"},
                )
                listing = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/list",
                    headers=headers,
                    json={"path": ".", "recursive": True},
                )
                search = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/search",
                    headers=headers,
                    json={"query": "value", "path": "src"},
                )
                read = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/read",
                    headers=headers,
                    json={"path": "src/example.py", "start_line": 1, "end_line": 1},
                )
                edit = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/edit",
                    headers=headers,
                    json={"path": "src/example.py", "target": "1", "replacement": "2"},
                )
                command = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/commands",
                    headers=headers,
                    json={"command": "printf direct-coding", "wait_seconds": 5},
                )
                command_id = command.json()["command_id"]
                command_status = client.get(
                    f"/api/control/v1/workspaces/ws_1/coding/commands/{command_id}?offset=0&wait_seconds=0",
                    headers=headers,
                )
                long_command = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/commands",
                    headers=headers,
                    json={"command": "sleep 5", "wait_seconds": 0},
                )
                long_command_id = long_command.json()["command_id"]
                cancelled = client.post(
                    f"/api/control/v1/workspaces/ws_1/coding/commands/{long_command_id}/cancel",
                    headers=headers,
                )
                cancelled_status = client.get(
                    f"/api/control/v1/workspaces/ws_1/coding/commands/{long_command_id}?offset=0&wait_seconds=2",
                    headers=headers,
                )

        self.assertEqual(write.status_code, 200)
        self.assertIn("src/example.py", listing.json()["entries"])
        self.assertIn("value = 1", search.json()["matches"])
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["content"], "value = 1\n")
        self.assertEqual(edit.status_code, 200)
        self.assertEqual(command.status_code, 200)
        self.assertEqual(command.json()["status"], "COMPLETE")
        self.assertIn("direct-coding", command.json()["output"])
        self.assertEqual(command_status.status_code, 200)
        self.assertEqual(command_status.json()["command_id"], command_id)
        self.assertEqual(long_command.status_code, 200)
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled_status.status_code, 200)
        self.assertEqual(cancelled_status.json()["status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()


class DirectCodingExternalCommandScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_command_requires_dedicated_scope(self):
        request = SimpleNamespace(state=SimpleNamespace(control_scopes={"command:execute"}))
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = CommandRequest(command="npm install example-package", allow_network=True)
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            self.assertRaises(HTTPException) as denied,
        ):
            await start_workspace_command(request, "ws_1", body)

        self.assertEqual(denied.exception.status_code, 403)
        self.assertIn("command:external", denied.exception.detail)


class ApiKeyScopeIssuanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_key_issuer_defaults_to_direct_coding_scopes_and_allows_explicit_external_scope(self):
        request = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"),
            cookies={},
        )
        saved: list[list[dict]] = []
        with (
            patch(
                "cptr.utils.config.check_access",
                return_value=SimpleNamespace(user_id="user_1"),
            ),
            patch("cptr.routers.gateway._get_api_keys", new=AsyncMock(return_value=[])),
            patch(
                "cptr.routers.gateway._save_api_keys",
                new=AsyncMock(side_effect=lambda keys: saved.append([dict(item) for item in keys])),
            ),
        ):
            default_result = await create_api_key(request, CreateApiKeyRequest(name="default"))
            external_result = await create_api_key(
                request,
                CreateApiKeyRequest(name="external", scopes=["coding:read", "command:external"]),
            )

        self.assertTrue(default_result["key"].startswith("sk-cptr-"))
        self.assertIn("coding:write", saved[0][0]["scopes"])
        self.assertNotIn("command:external", saved[0][0]["scopes"])
        self.assertEqual(saved[1][-1]["scopes"], ["coding:read", "command:external"])
        self.assertTrue(external_result["key"].startswith("sk-cptr-"))

    async def test_key_issuer_rejects_unknown_scope(self):
        request = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"),
            cookies={},
        )
        with (
            patch(
                "cptr.utils.config.check_access",
                return_value=SimpleNamespace(user_id="user_1"),
            ),
            self.assertRaises(HTTPException) as rejected,
        ):
            await create_api_key(
                request,
                CreateApiKeyRequest(name="invalid", scopes=["workspace:root"]),
            )

        self.assertEqual(rejected.exception.status_code, 422)


class DirectCodingHttpAuthorizationTests(unittest.TestCase):
    def test_write_is_denied_when_a_real_bearer_token_lacks_coding_write(self):
        app = FastAPI()
        app.include_router(coding_router)
        token = "read-only-direct-coding-token"
        key = {
            "key_hash": hashlib.sha256(token.encode()).hexdigest(),
            "user_id": "user_1",
            "scopes": ["coding:read"],
        }
        headers = {"Authorization": f"Bearer {token}"}

        with tempfile.TemporaryDirectory() as workspace_root:
            workspace = SimpleNamespace(path=workspace_root, user_id="user_1")
            with (
                patch(
                    "cptr.services.control_auth._get_api_keys",
                    new=AsyncMock(return_value=[key]),
                ),
                patch(
                    "cptr.services.control_auth.Auth.get_by_user_id",
                    new=AsyncMock(return_value=SimpleNamespace(username="tester")),
                ),
                patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/write",
                    headers=headers,
                    json={"path": "src/example.py", "content": "value = 1\n"},
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "missing required scope: coding:write")
