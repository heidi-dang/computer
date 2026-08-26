import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from cptr.routers.coding import (
    CommandRequest,
    EditRequest,
    WriteRequest,
    edit_workspace_file,
    start_workspace_command,
    write_workspace_file,
)
from cptr.routers.coding import router as coding_router
from cptr.routers.gateway import CreateApiKeyRequest, create_api_key


class LegacyReadCompatibilityTests(unittest.TestCase):
    def test_production_read_route_remains_scoped_and_agent_free(self):
        token = "legacy-read-token"
        key = {
            "key_hash": hashlib.sha256(token.encode()).hexdigest(),
            "user_id": "user_1",
            "scopes": ["coding:read"],
        }
        headers = {"Authorization": f"Bearer {token}"}
        app = FastAPI()
        app.include_router(coding_router)

        with tempfile.TemporaryDirectory() as workspace_root:
            Path(workspace_root, "example.py").write_text("value = 1\n", encoding="utf-8")
            workspace = SimpleNamespace(path=workspace_root, user_id="user_1")
            with (
                patch("cptr.services.control_auth._get_api_keys", new=AsyncMock(return_value=[key])),
                patch(
                    "cptr.services.control_auth.Auth.get_by_user_id",
                    new=AsyncMock(return_value=SimpleNamespace(username="tester")),
                ),
                patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/read",
                    headers=headers,
                    json={"path": "example.py"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "value = 1\n")


class LegacyMutationRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_side_effect_endpoints_are_retired_before_access_or_execution(self):
        request = SimpleNamespace()
        calls = [
            write_workspace_file(request, "ws_1", WriteRequest(path="a.py", content="x")),
            edit_workspace_file(request, "ws_1", EditRequest(path="a.py", target="x", replacement="y")),
            start_workspace_command(request, "ws_1", CommandRequest(command="python3 -c 'import os'")),
        ]
        for call in calls:
            with self.assertRaises(HTTPException) as retired:
                await call
            self.assertEqual(retired.exception.status_code, 410)


class ApiKeyScopeIssuanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_key_excludes_direct_mutation_and_execution_scopes(self):
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), cookies={})
        saved: list[list[dict]] = []
        with (
            patch("cptr.utils.config.check_access", return_value=SimpleNamespace(user_id="user_1")),
            patch("cptr.routers.gateway._get_api_keys", new=AsyncMock(return_value=[])),
            patch(
                "cptr.routers.gateway._save_api_keys",
                new=AsyncMock(side_effect=lambda keys: saved.append([dict(item) for item in keys])),
            ),
        ):
            await create_api_key(request, CreateApiKeyRequest(name="default"))
            await create_api_key(
                request,
                CreateApiKeyRequest(
                    name="direct",
                    scopes=["direct:inspect", "direct:mutate", "direct:execute"],
                ),
            )

        self.assertNotIn("direct:mutate", saved[0][0]["scopes"])
        self.assertNotIn("command:execute", saved[0][0]["scopes"])
        self.assertEqual(saved[1][-1]["scopes"], ["direct:inspect", "direct:mutate", "direct:execute"])


if __name__ == "__main__":
    unittest.main()
