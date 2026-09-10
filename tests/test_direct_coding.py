import asyncio
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
    ApplyEditsRequest,
    CommandRequest,
    EditRequest,
    ReadRequest,
    WriteRequest,
    SecretWriteRequest,
    TestTargetRequest as CodingTestTargetRequest,
    WorkspaceInspectRequest,
    _relative_path,
    _validate_command,
    apply_workspace_edits,
    edit_workspace_file,
    inspect_workspace,
    read_workspace_file,
    run_workspace_test_target,
    start_workspace_command,
    write_workspace_file,
    write_workspace_secret,
    _cursor,
    _sha256,
)
from cptr.routers.coding import router as coding_router
from cptr.routers.gateway import CreateApiKeyRequest, create_api_key
from cptr.services.api_keys import ApiKeyPrincipal
from cptr.services.live_events import LiveEventHub, LiveEventStore, command_target_key
from cptr.utils.tools import command_sessions, run_command, search_files, stop_command_session


class SearchFilesFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_python_fallback_matches_ripgrep_line_contract_without_separator_whitespace(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.Runtime.stat",
                    new=AsyncMock(return_value={"type": "directory"}),
                ),
                patch(
                    "cptr.utils.tools._search_rg",
                    new=AsyncMock(side_effect=FileNotFoundError()),
                ),
                patch(
                    "cptr.utils.tools.Runtime.file_matches",
                    new=AsyncMock(
                        return_value={
                            "results": [
                                {
                                    "relative_path": "example.py",
                                    "content_matches": [
                                        {"line": 1, "text": "value = 1"},
                                        {"line": 2, "text": "    indented"},
                                    ],
                                }
                            ]
                        }
                    ),
                ),
                patch(
                    "cptr.utils.tools.identity_for_context",
                    new=AsyncMock(return_value=SimpleNamespace(is_pam=False)),
                ),
            ):
                result = await search_files(
                    "value",
                    ".",
                    __context__={
                        "workspace": workspace_root,
                        "request": object(),
                        "user_id": "user-1",
                    },
                )

        self.assertEqual(
            result.splitlines(),
            ["example.py:1:value = 1", "example.py:2:    indented"],
        )


class DirectCodingContractHelperTests(unittest.TestCase):
    def test_cursor_rejects_malformed_values_as_typed_bad_request(self):
        with self.assertRaises(HTTPException) as caught:
            _cursor("not-a-cursor")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail["code"], "INVALID_CURSOR")

    def test_content_hash_is_over_full_content_independent_of_slice(self):
        content = "alpha\nbeta\ngamma\n"
        self.assertEqual(_sha256(content), hashlib.sha256(content.encode()).hexdigest())
        self.assertNotEqual(_sha256("beta\n"), _sha256(content))


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
                    "cptr.services.control_auth.resolve_api_key_principal",
                    new=AsyncMock(
                        return_value=ApiKeyPrincipal(
                            user_id="user_1",
                            username="tester",
                            scopes=frozenset(key["scopes"]),
                        )
                    ),
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
                    "cptr.services.control_auth.resolve_api_key_principal",
                    new=AsyncMock(
                        return_value=ApiKeyPrincipal(
                            user_id="user_1",
                            username="tester",
                            scopes=frozenset(key["scopes"]),
                        )
                    ),
                ),
                patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            ):
                # This test verifies Socket.IO ASGI dispatch, not application startup.
                # Entering TestClient as a context manager runs the production
                # lifespan and can migrate the operator's shared CPTR database,
                # making this route-registration test non-hermetic.
                client = TestClient(cptr_application)
                response = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/read",
                    headers=headers,
                    json={"path": "wrapped.py"},
                )
                client.close()

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

    async def test_read_automatically_enriches_supported_source_with_lsp(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = ReadRequest(path="src/app.py")
        intelligence = {
            "provider": "lsp",
            "server_id": "pyright",
            "status": "ok",
            "symbols": [{"name": "f"}],
            "diagnostics": [],
        }
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.Runtime.read_text_file",
                new=AsyncMock(
                    return_value={
                        "binary": False,
                        "size": 24,
                        "content": "def f():\n    return 1\n",
                    }
                ),
            ),
            patch(
                "cptr.routers.coding.identity_for_context",
                new=AsyncMock(return_value=SimpleNamespace(is_pam=False)),
            ),
            patch(
                "cptr.routers.coding.automatic_lsp_intelligence_service.enrich_read",
                new=AsyncMock(return_value=intelligence),
            ) as enrich,
        ):
            result = await read_workspace_file(request, "ws_1", body)

        self.assertEqual(result["intelligence"], intelligence)
        enrich.assert_awaited_once()
        kwargs = enrich.await_args.kwargs
        self.assertEqual(kwargs["root"], Path("/tmp/cptr-direct-coding"))
        self.assertEqual(kwargs["path"], Path("/tmp/cptr-direct-coding/src/app.py"))
        self.assertEqual(kwargs["content"], "def f():\n    return 1\n")

    async def test_write_succeeds_when_automatic_lsp_enrichment_degrades(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = WriteRequest(path="src/app.py", content="value = 2\n", overwrite=True)
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.Runtime.read_file",
                new=AsyncMock(return_value={"binary": False, "content": "value = 1\n"}),
            ),
            patch("cptr.routers.coding.Runtime.write_file", new=AsyncMock(return_value={})),
            patch(
                "cptr.routers.coding.identity_for_context",
                new=AsyncMock(return_value=SimpleNamespace(is_pam=False)),
            ),
            patch(
                "cptr.routers.coding.automatic_lsp_intelligence_service.enrich_after_write",
                new=AsyncMock(side_effect=RuntimeError("synthetic lsp outage")),
            ),
        ):
            result = await write_workspace_file(request, "ws_1", body)

        self.assertEqual(result["sha256"], _sha256("value = 2\n"))
        self.assertEqual(result["intelligence"]["provider"], "lsp")
        self.assertEqual(result["intelligence"]["status"], "degraded")

    async def test_secret_write_requires_exact_prompt_approval_and_owned_workbench(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = SecretWriteRequest(
            path=".env",
            secret="PASSWORD=synthetic-test-secret\n",
            workbench_session_id="wbs_1234567890abcdef",
        )
        private_write = AsyncMock(return_value={"status": "saved"})
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.Runtime.write_private_file", new=private_write),
            self.assertRaises(HTTPException) as denied,
        ):
            await write_workspace_secret(request, "ws_1", body)

        self.assertEqual(denied.exception.status_code, 403)
        private_write.assert_not_awaited()

    async def test_prompt_approved_secret_write_can_materialize_dotenv_without_exposing_secret_metadata(
        self,
    ):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = SecretWriteRequest(
            path=".env",
            secret="PASSWORD=synthetic-test-secret\n",
            workbench_session_id="wbs_1234567890abcdef",
            user_approval="allow:secret-write",
            overwrite=True,
        )
        session = {
            "session_id": "wbs_1234567890abcdef",
            "workspace_id": "ws_1",
            "status": "OPEN",
            "archived_at": None,
        }
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.workbench_session_store.get",
                new=AsyncMock(return_value=session),
            ) as get_session,
            patch(
                "cptr.routers.coding.Runtime.write_private_file",
                new=AsyncMock(return_value={"status": "saved"}),
            ) as private_write,
        ):
            result = await write_workspace_secret(request, "ws_1", body)

        get_session.assert_awaited_once_with(owner_id="user_1", session_id="wbs_1234567890abcdef")
        private_write.assert_awaited_once_with(
            request,
            "/tmp/cptr-direct-coding/.env",
            "PASSWORD=synthetic-test-secret\n",
            overwrite=True,
        )
        self.assertEqual(
            result,
            {
                "workspace_id": "ws_1",
                "path": ".env",
                "scope": "workspace",
                "materialized": True,
                "permissions": "0600",
            },
        )
        self.assertNotIn("synthetic-test-secret", repr(result))
        self.assertNotIn("sha256", result)
        self.assertNotIn("bytes_written", result)
        with self.assertRaises(HTTPException):
            _relative_path(".env", Path("/tmp/cptr-direct-coding").resolve())

    async def test_host_secret_write_requires_active_root_grant_and_uses_root_identity(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = SecretWriteRequest(
            path="/etc/cptr/example.secret",
            secret="synthetic-test-secret\n",
            workbench_session_id="wbs_1234567890abcdef",
            user_approval="allow:secret-write",
            overwrite=True,
        )
        session = {
            "session_id": "wbs_1234567890abcdef",
            "workspace_id": "ws_1",
            "status": "OPEN",
            "archived_at": None,
        }
        root_identity = SimpleNamespace(app_user_id="user_1", is_pam=False, uid=0)
        private_write = AsyncMock(return_value={"status": "saved"})
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.workbench_session_store.get",
                new=AsyncMock(return_value=session),
            ),
            patch("cptr.routers.coding.local_root_grants_enabled", return_value=True),
            patch(
                "cptr.routers.coding.local_root_grant_store.is_active",
                new=AsyncMock(return_value=True),
            ) as root_active,
            patch(
                "cptr.routers.coding.identity_for_context",
                new=AsyncMock(return_value=root_identity),
            ),
            patch(
                "cptr.routers.coding.unrestricted_root_identity",
                return_value=root_identity,
            ),
            patch("cptr.routers.coding.Runtime.write_private_file_as", new=private_write),
        ):
            result = await write_workspace_secret(request, "ws_1", body)

        root_active.assert_awaited_once_with(owner_id="user_1", session_id="wbs_1234567890abcdef")
        private_write.assert_awaited_once_with(
            root_identity,
            "/etc/cptr/example.secret",
            "synthetic-test-secret\n",
            overwrite=True,
        )
        self.assertEqual(result["scope"], "host-root")
        self.assertEqual(result["path"], "/etc/cptr/example.secret")
        self.assertNotIn("synthetic-test-secret", repr(result))

    async def test_host_secret_write_is_denied_without_active_root_grant(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = SecretWriteRequest(
            path="/etc/cptr/example.secret",
            secret="synthetic-test-secret\n",
            workbench_session_id="wbs_1234567890abcdef",
            user_approval="allow:secret-write",
        )
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.workbench_session_store.get",
                new=AsyncMock(
                    return_value={
                        "session_id": "wbs_1234567890abcdef",
                        "workspace_id": "ws_1",
                        "status": "OPEN",
                        "archived_at": None,
                    }
                ),
            ),
            patch("cptr.routers.coding.local_root_grants_enabled", return_value=True),
            patch(
                "cptr.routers.coding.local_root_grant_store.is_active",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "cptr.routers.coding.Runtime.write_private_file_as",
                new=AsyncMock(return_value={"status": "saved"}),
            ) as private_write,
            self.assertRaises(HTTPException) as denied,
        ):
            await write_workspace_secret(request, "ws_1", body)

        self.assertEqual(denied.exception.status_code, 403)
        private_write.assert_not_awaited()

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
                new=AsyncMock(
                    return_value={"binary": False, "content": "def f():\n    return 'old'\n"}
                ),
            ) as read_file,
            patch(
                "cptr.routers.coding.Runtime.write_file", new=AsyncMock(return_value={})
            ) as write_file,
            patch(
                "cptr.routers.coding.identity_for_context",
                new=AsyncMock(return_value=SimpleNamespace(is_pam=False)),
            ),
            patch(
                "cptr.routers.coding.automatic_lsp_intelligence_service.enrich_after_write",
                new=AsyncMock(
                    return_value={"provider": "lsp", "server_id": "pyright", "status": "ok"}
                ),
            ) as enrich,
        ):
            result = await edit_workspace_file(request, "ws_1", body)

        self.assertEqual(result["path"], "src/app.py")
        self.assertEqual(result["intelligence"]["status"], "ok")
        enrich.assert_awaited_once()
        read_file.assert_awaited_once()
        write_file.assert_awaited_once_with(
            request,
            "/tmp/cptr-direct-coding/src/app.py",
            "def f():\n    return 'new'\n",
        )

    async def test_apply_edits_uses_original_spans_so_replacements_cannot_capture_later_targets(
        self,
    ):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = ApplyEditsRequest(
            path="src/app.py",
            edits=[
                {"target": "first", "replacement": "second"},
                {"target": "second", "replacement": "done"},
            ],
        )
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.Runtime.read_file",
                new=AsyncMock(return_value={"binary": False, "content": "first\nsecond\n"}),
            ),
            patch(
                "cptr.routers.coding.Runtime.write_file", new=AsyncMock(return_value={})
            ) as write_file,
            patch(
                "cptr.routers.coding.identity_for_context",
                new=AsyncMock(return_value=SimpleNamespace(is_pam=False)),
            ),
            patch(
                "cptr.routers.coding.automatic_lsp_intelligence_service.enrich_after_write",
                new=AsyncMock(
                    return_value={"provider": "lsp", "server_id": "pyright", "status": "ok"}
                ),
            ) as enrich,
        ):
            result = await apply_workspace_edits(request, "ws_1", body)

        write_file.assert_awaited_once_with(
            request,
            "/tmp/cptr-direct-coding/src/app.py",
            "second\ndone\n",
        )
        self.assertEqual(result["sha256"], _sha256("second\ndone\n"))
        self.assertEqual(result["intelligence"]["status"], "ok")
        enrich.assert_awaited_once()
        self.assertIn("-first", result["diff"])
        self.assertIn("+done", result["diff"])

    async def test_direct_command_uses_no_model_or_agent_inputs(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = CommandRequest(command="npm test", cwd=".", wait_seconds=5)
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.run_command",
                new=AsyncMock(return_value="Task deadbeef: exited (code 0)"),
            ) as run,
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
                "workspace_id": "ws_1",
                "request": request,
                "user_id": "user_1",
            },
            __use_pty=False,
        )

    async def test_direct_command_validates_and_routes_owned_workbench_before_execution(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = CommandRequest(
            command="npm test",
            cwd=".",
            wait_seconds=5,
            workbench_session_id="wbs_1234567890abcdef",
        )
        session = {
            "session_id": "wbs_1234567890abcdef",
            "workspace_id": "ws_1",
            "status": "OPEN",
            "archived_at": None,
        }
        get_workbench = AsyncMock(return_value=session)
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.workbench_session_store", create=True) as store,
            patch(
                "cptr.routers.coding.run_command",
                new=AsyncMock(return_value="Task deadbeef: exited (code 0)"),
            ) as run,
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
            store.get = get_workbench
            result = await start_workspace_command(request, "ws_1", body)

        self.assertEqual(result["status"], "COMPLETE")
        get_workbench.assert_awaited_once_with(owner_id="user_1", session_id="wbs_1234567890abcdef")
        self.assertEqual(
            run.await_args.kwargs["__context__"]["workbench_session_id"],
            "wbs_1234567890abcdef",
        )

    async def test_archived_workbench_route_is_rejected_before_command_spawn(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = CommandRequest(
            command="npm test",
            workbench_session_id="wbs_1234567890abcdef",
        )
        run = AsyncMock(return_value="Task deadbeef: running")
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.workbench_session_store", create=True) as store,
            patch("cptr.routers.coding.run_command", new=run),
        ):
            store.get = AsyncMock(
                return_value={
                    "session_id": "wbs_1234567890abcdef",
                    "workspace_id": "ws_1",
                    "status": "ARCHIVED",
                    "archived_at": 1,
                }
            )
            with self.assertRaises(HTTPException) as rejected:
                await start_workspace_command(request, "ws_1", body)

        self.assertEqual(rejected.exception.status_code, 409)
        run.assert_not_awaited()

    async def test_completed_high_value_command_is_offered_to_memory_observation_once(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = CommandRequest(command="npm test", cwd=".", wait_seconds=5)
        session = {"workspace": "/tmp/cptr-direct-coding", "command": "npm test"}
        snapshot = {
            "command_id": "deadbeef",
            "status": "COMPLETE",
            "exit_code": 0,
            "output": "tests pass",
            "next_offset": 10,
        }
        observe = AsyncMock(return_value="job-1")
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.run_command",
                new=AsyncMock(return_value="Task deadbeef: exited (code 0)"),
            ),
            patch("cptr.routers.coding.get_command_session", return_value=session),
            patch("cptr.routers.coding._command_snapshot", new=AsyncMock(return_value=snapshot)),
            patch("cptr.routers.coding.observe_execution_outcome", new=observe),
        ):
            result = await start_workspace_command(request, "ws_1", body)

        self.assertEqual(result["status"], "COMPLETE")
        observe.assert_awaited_once_with(
            user_id="user_1",
            workspace="/tmp/cptr-direct-coding",
            action="command",
            command="npm test",
            status="COMPLETE",
            exit_code=0,
            output="tests pass",
            metadata={"transport": "local-command", "privilege": "user"},
        )
        self.assertTrue(session["memory_observation_checked"])

    async def test_direct_command_marks_initial_wait_timeout_when_process_is_still_running(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = CommandRequest(command="python -m pytest", cwd=".", wait_seconds=5)
        snapshot = {
            "command_id": "deadbeef",
            "status": "RUNNING",
            "exit_code": None,
            "output": "",
            "next_offset": 0,
            "duration_ms": 5000,
            "output_truncated": False,
            "timed_out": False,
        }
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.run_command",
                new=AsyncMock(
                    return_value="Task deadbeef: running\nCommand: python -m pytest\nnext_offset: 0\n---\n"
                ),
            ),
            patch(
                "cptr.routers.coding._command_snapshot",
                new=AsyncMock(return_value=snapshot.copy()),
            ),
        ):
            result = await start_workspace_command(request, "ws_1", body)

        self.assertEqual(result["status"], "RUNNING")
        self.assertTrue(result["timed_out"])

    async def test_workspace_inspection_uses_direct_workspace_scope(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = WorkspaceInspectRequest(kind="project")
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding._workspace_insight",
                new=AsyncMock(
                    return_value={
                        "project_files": ["package.json"],
                        "detected_runtimes": ["node"],
                        "root": ".",
                    }
                ),
            ) as insight,
        ):
            result = await inspect_workspace(request, "ws_1", body)

        self.assertEqual(result["workspace_id"], "ws_1")
        self.assertEqual(result["kind"], "project")
        self.assertEqual(result["detected_runtimes"], ["node"])
        insight.assert_awaited_once()

    async def test_structured_test_target_maps_to_fixed_command_profile(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = CodingTestTargetRequest(target="node_build", path=".", wait_seconds=0)
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.run_command",
                new=AsyncMock(return_value="Task deadbeef: running"),
            ) as run,
            patch(
                "cptr.routers.coding._command_snapshot",
                new=AsyncMock(
                    return_value={
                        "command_id": "deadbeef",
                        "status": "RUNNING",
                        "exit_code": None,
                        "output": "",
                        "next_offset": 0,
                        "duration_ms": 0,
                        "output_truncated": False,
                        "timed_out": False,
                    }
                ),
            ),
        ):
            result = await run_workspace_test_target(request, "ws_1", body)

        self.assertEqual(result["target"], "node_build")
        run.assert_awaited_once_with(
            "npm run build",
            ".",
            0,
            __context__={
                "workspace": "/tmp/cptr-direct-coding",
                "workspace_id": "ws_1",
                "request": request,
                "user_id": "user_1",
            },
            __argv=["npm", "run", "build"],
            __use_pty=False,
        )

    async def test_python_test_target_uses_current_interpreter(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = CodingTestTargetRequest(
            target="python_pytest",
            path=".",
            test_path="tests/test_smoke.py",
            wait_seconds=0,
        )
        with (
            patch("cptr.routers.coding.sys.executable", "/opt/cptr/python"),
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.run_command",
                new=AsyncMock(return_value="Task deadbeef: running"),
            ) as run,
            patch(
                "cptr.routers.coding._command_snapshot",
                new=AsyncMock(
                    return_value={
                        "command_id": "deadbeef",
                        "status": "RUNNING",
                        "exit_code": None,
                        "output": "",
                        "next_offset": 0,
                        "duration_ms": 0,
                        "output_truncated": False,
                        "timed_out": False,
                    }
                ),
            ),
        ):
            result = await run_workspace_test_target(request, "ws_1", body)

        self.assertEqual(result["target"], "python_pytest")
        run.assert_awaited_once_with(
            "/opt/cptr/python -m pytest tests/test_smoke.py",
            ".",
            0,
            __context__={
                "workspace": "/tmp/cptr-direct-coding",
                "workspace_id": "ws_1",
                "request": request,
                "user_id": "user_1",
            },
            __argv=["/opt/cptr/python", "-m", "pytest", "tests/test_smoke.py"],
            __use_pty=False,
        )

    async def test_run_command_publishes_real_incremental_live_terminal_events(self):
        hub = LiveEventHub(store=LiveEventStore())
        request = SimpleNamespace()
        identity = SimpleNamespace(is_pam=False, app_user_id="user_1")
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context", new=AsyncMock(return_value=identity)
                ),
                patch("cptr.utils.tools.Runtime.write_file", new=AsyncMock(return_value={})),
                patch("cptr.services.live_events.live_event_hub", hub),
            ):
                result = await run_command(
                    "printf first; sleep 0.05; printf second",
                    ".",
                    2,
                    __context__={
                        "workspace": workspace_root,
                        "workspace_id": "ws_1",
                        "request": request,
                        "user_id": "user_1",
                    },
                    __use_pty=False,
                )

        command_id = result.split(":", 1)[0].removeprefix("Task ")
        try:
            events = await hub.store.replay(command_target_key("ws_1", command_id))
            types = [event.event_type for event in events]
            self.assertEqual(types[0], "command.started")
            self.assertEqual(types[-1], "command.completed")
            self.assertEqual(types.count("command.started"), 1)
            self.assertEqual(types.count("command.completed"), 1)
            self.assertIn("terminal.chunk", types)
            combined = "".join(
                str(event.payload.get("text") or "")
                for event in events
                if event.event_type == "terminal.chunk"
            )
            self.assertIn("first", combined)
            self.assertIn("second", combined)
            self.assertEqual(events[-1].payload["status"], "COMPLETE")
            self.assertEqual(events[-1].payload["exit_code"], 0)
        finally:
            command_sessions.pop(command_id, None)

    async def test_run_command_binds_workbench_target_before_live_events(self):
        hub = LiveEventHub(store=LiveEventStore())
        request = SimpleNamespace()
        identity = SimpleNamespace(is_pam=False, app_user_id="user_1")
        bind_target = AsyncMock(return_value={"session_id": "wbs_1234567890abcdef"})
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context", new=AsyncMock(return_value=identity)
                ),
                patch("cptr.utils.tools.Runtime.write_file", new=AsyncMock(return_value={})),
                patch("cptr.services.live_events.live_event_hub", hub),
                patch(
                    "cptr.services.workbench_sessions.workbench_session_store.bind_target",
                    new=bind_target,
                ),
            ):
                result = await run_command(
                    "printf bound",
                    ".",
                    2,
                    __context__={
                        "workspace": workspace_root,
                        "workspace_id": "ws_1",
                        "workbench_session_id": "wbs_1234567890abcdef",
                        "request": request,
                        "user_id": "user_1",
                    },
                    __use_pty=False,
                )

        command_id = result.split(":", 1)[0].removeprefix("Task ")
        try:
            bind_target.assert_awaited_once_with(
                owner_id="user_1",
                session_id="wbs_1234567890abcdef",
                target_type="command",
                target_id=command_id,
                workspace_id="ws_1",
            )
        finally:
            command_sessions.pop(command_id, None)

    async def test_run_command_projects_runtime_bytes_into_workbench_stream(self):
        hub = LiveEventHub(store=LiveEventStore())
        request = SimpleNamespace()
        identity = SimpleNamespace(is_pam=False, app_user_id="user_1")
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context", new=AsyncMock(return_value=identity)
                ),
                patch("cptr.utils.tools.Runtime.write_file", new=AsyncMock(return_value={})),
                patch("cptr.services.live_events.live_event_hub", hub),
            ):
                result = await run_command(
                    "printf workbench-output",
                    ".",
                    2,
                    __context__={
                        "workspace": workspace_root,
                        "workspace_id": "ws_1",
                        "workbench_session_id": "wbs_1234567890abcdef",
                        "request": request,
                        "user_id": "user_1",
                    },
                    __use_pty=False,
                )

        command_id = result.split(":", 1)[0].removeprefix("Task ")
        try:
            events = await hub.store.replay("workbench:wbs_1234567890abcdef")
            self.assertEqual(events[0].event_type, "command.started")
            self.assertEqual(events[-1].event_type, "command.completed")
            self.assertTrue(all(event.payload["target"]["id"] == command_id for event in events))
            self.assertIn(
                "workbench-output",
                "".join(
                    str(event.payload["payload"].get("text") or "")
                    for event in events
                    if event.event_type == "terminal.chunk"
                ),
            )
        finally:
            command_sessions.pop(command_id, None)

    async def test_run_command_projects_runtime_bytes_into_parent_task_stream(self):
        hub = LiveEventHub(store=LiveEventStore())
        request = SimpleNamespace()
        identity = SimpleNamespace(is_pam=False, app_user_id="user_1")
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context", new=AsyncMock(return_value=identity)
                ),
                patch("cptr.utils.tools.Runtime.write_file", new=AsyncMock(return_value={})),
                patch("cptr.services.live_events.live_event_hub", hub),
            ):
                result = await run_command(
                    "printf agent-output",
                    ".",
                    2,
                    __context__={
                        "workspace": workspace_root,
                        "control_task_id": "task-1",
                        "request": request,
                        "user_id": "user_1",
                    },
                )

        command_id = result.split(":", 1)[0].removeprefix("Task ")
        try:
            events = await hub.store.replay("task:task-1")
            self.assertEqual([event.event_type for event in events][0], "command.started")
            self.assertEqual([event.event_type for event in events][-1], "command.completed")
            self.assertIn(
                "agent-output",
                "".join(
                    str(event.payload.get("text") or "")
                    for event in events
                    if event.event_type == "terminal.chunk"
                ),
            )
            self.assertTrue(all(event.target_key == "task:task-1" for event in events))
        finally:
            command_sessions.pop(command_id, None)

    async def test_run_command_publishes_truthful_nonzero_exit_status(self):
        hub = LiveEventHub(store=LiveEventStore())
        request = SimpleNamespace()
        identity = SimpleNamespace(is_pam=False, app_user_id="user_1")
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context", new=AsyncMock(return_value=identity)
                ),
                patch("cptr.utils.tools.Runtime.write_file", new=AsyncMock(return_value={})),
                patch("cptr.services.live_events.live_event_hub", hub),
            ):
                result = await run_command(
                    "printf failing-output; exit 7",
                    ".",
                    2,
                    __context__={
                        "workspace": workspace_root,
                        "workspace_id": "ws_1",
                        "request": request,
                        "user_id": "user_1",
                    },
                )

        command_id = result.split(":", 1)[0].removeprefix("Task ")
        try:
            events = await hub.store.replay(command_target_key("ws_1", command_id))
            completed = [event for event in events if event.event_type == "command.completed"]
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0].payload["status"], "FAILED")
            self.assertEqual(completed[0].payload["exit_code"], 7)
            self.assertIn(
                "failing-output",
                "".join(
                    str(event.payload.get("text") or "")
                    for event in events
                    if event.event_type == "terminal.chunk"
                ),
            )
        finally:
            command_sessions.pop(command_id, None)

    async def test_cancelled_command_finishes_live_stream_with_real_process_exit(self):
        hub = LiveEventHub(store=LiveEventStore())
        request = SimpleNamespace()
        identity = SimpleNamespace(is_pam=False, app_user_id="user_1")
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context", new=AsyncMock(return_value=identity)
                ),
                patch("cptr.utils.tools.Runtime.write_file", new=AsyncMock(return_value={})),
                patch("cptr.services.live_events.live_event_hub", hub),
            ):
                result = await run_command(
                    "printf cancellation-started; sleep 5",
                    ".",
                    0,
                    __context__={
                        "workspace": workspace_root,
                        "workspace_id": "ws_1",
                        "request": request,
                        "user_id": "user_1",
                    },
                    __use_pty=False,
                )
                command_id = result.split(":", 1)[0].removeprefix("Task ")
                self.assertIsNone(stop_command_session(request, command_id))
                log_task = command_sessions[command_id]["log_task"]
                await asyncio.wait_for(asyncio.shield(log_task), timeout=2)

        try:
            events = await hub.store.replay(command_target_key("ws_1", command_id))
            types = [event.event_type for event in events]
            self.assertEqual(types.count("command.started"), 1)
            self.assertEqual(types.count("command.completed"), 1)
            completed = next(event for event in events if event.event_type == "command.completed")
            self.assertEqual(completed.payload["status"], "FAILED")
            self.assertNotEqual(completed.payload["exit_code"], 0)
        finally:
            command_sessions.pop(command_id, None)


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
        hub = LiveEventHub(store=LiveEventStore())

        with tempfile.TemporaryDirectory() as workspace_root:
            workspace = SimpleNamespace(path=workspace_root, user_id="user_1")
            with (
                patch(
                    "cptr.services.control_auth.resolve_api_key_principal",
                    new=AsyncMock(
                        return_value=ApiKeyPrincipal(
                            user_id="user_1",
                            username="tester",
                            scopes=frozenset(key["scopes"]),
                        )
                    ),
                ),
                patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
                patch(
                    "cptr.services.control_auth.require_control_action_memory",
                    new=AsyncMock(return_value=SimpleNamespace(context_id="memctx-direct-coding")),
                ),
                patch("cptr.services.live_events.live_event_hub", hub),
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
                directory = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/directories",
                    headers=headers,
                    json={"path": "generated"},
                )
                moved = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/move",
                    headers=headers,
                    json={"source": "src/example.py", "destination": "generated/example.py"},
                )
                deleted = client.post(
                    "/api/control/v1/workspaces/ws_1/coding/delete",
                    headers=headers,
                    json={"path": "generated/example.py"},
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
        listed_file = next(
            item for item in listing.json()["entries"] if item["path"] == "src/example.py"
        )
        self.assertEqual(listed_file["type"], "file")
        self.assertEqual(listed_file["size"], len("value = 1\n"))
        self.assertIsNotNone(listed_file["modified"])
        self.assertIn(
            {"path": "example.py", "line": 1, "text": "value = 1"},
            search.json()["matches"],
        )
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["content"], "value = 1\n")
        self.assertEqual(edit.status_code, 200)
        self.assertEqual(directory.status_code, 200)
        self.assertEqual(directory.json()["type"], "directory")
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.json()["destination"], "generated/example.py")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])
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
    async def test_key_issuer_defaults_to_direct_coding_scopes_and_allows_explicit_external_scope(
        self,
    ):
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
                    "cptr.services.control_auth.resolve_api_key_principal",
                    new=AsyncMock(
                        return_value=ApiKeyPrincipal(
                            user_id="user_1",
                            username="tester",
                            scopes=frozenset(key["scopes"]),
                        )
                    ),
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
        self.assertEqual(
            response.json()["detail"],
            {
                "code": "CONTROL_SCOPE_REQUIRED",
                "message": "missing required scope: coding:write",
                "retriable": False,
            },
        )
