import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from cptr.routers.coding import CommandRequest, _command_request_fingerprint, _validate_command, start_workspace_command
from cptr.services.local_root_grants import ROOT_GRANT_MARKER, ROOT_REVOKE_MARKER
from cptr.utils.identity import IdentityUnavailable


class LocalRootCommandPolicyTests(unittest.TestCase):
    def test_root_mode_bypasses_only_local_destructive_classifier(self):
        _validate_command("rm -rf build", False, root_unrestricted=True)

    def test_root_mode_does_not_bypass_network_or_ssh_authorization(self):
        with self.assertRaises(HTTPException) as network_denied:
            _validate_command("npm install example-package", False, root_unrestricted=True)
        self.assertEqual(network_denied.exception.status_code, 403)
        with self.assertRaises(HTTPException) as ssh_denied:
            _validate_command("ssh example-host", True, root_unrestricted=True)
        self.assertEqual(ssh_denied.exception.status_code, 403)

    def test_nul_is_rejected_even_in_root_mode(self):
        with self.assertRaises(HTTPException) as denied:
            _validate_command("printf ok\x00bad", False, root_unrestricted=True)
        self.assertEqual(denied.exception.status_code, 422)

    def test_idempotency_fingerprint_distinguishes_root_authority(self):
        body = CommandRequest(
            command="printf ok",
            workbench_session_id="wbs_1234567890abcdef",
        )
        self.assertNotEqual(
            _command_request_fingerprint(body, ".", root_unrestricted=False),
            _command_request_fingerprint(body, ".", root_unrestricted=True),
        )


class LocalRootCommandRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root_feature_patch = patch(
            "cptr.routers.coding.local_root_grants_enabled", return_value=True
        )
        self.root_feature_patch.start()
        self.addCleanup(self.root_feature_patch.stop)

    @staticmethod
    def _request():
        return SimpleNamespace(state=SimpleNamespace(control_scopes={"command:execute"}))

    @staticmethod
    def _snapshot():
        return {
            "command_id": "deadbeef",
            "status": "COMPLETE",
            "exit_code": 0,
            "output": "ok",
            "next_offset": 2,
            "duration_ms": 1,
            "output_truncated": False,
            "timed_out": False,
        }

    async def test_host_operator_must_enable_root_grants(self):
        request = self._request()
        workspace = SimpleNamespace(path="/tmp/cptr-root-workspace")
        body = CommandRequest(
            command=f"{ROOT_GRANT_MARKER}\nprintf ok",
            workbench_session_id="wbs_1234567890abcdef",
        )
        grant = AsyncMock(return_value={})
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.local_root_grants_enabled", return_value=False),
            patch("cptr.routers.coding.local_root_grant_store.grant", new=grant),
            self.assertRaises(HTTPException) as denied,
        ):
            await start_workspace_command(request, "ws-1", body)
        self.assertEqual(denied.exception.status_code, 403)
        grant.assert_not_awaited()

    async def test_explicit_grant_strips_marker_and_runs_with_root_context(self):
        request = self._request()
        workspace = SimpleNamespace(path="/tmp/cptr-root-workspace")
        root_identity = SimpleNamespace(app_user_id="user-1", is_pam=False, uid=0)
        body = CommandRequest(
            command=f"{ROOT_GRANT_MARKER}\nrm -rf build",
            workbench_session_id="wbs_1234567890abcdef",
            wait_seconds=0,
        )
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.identity_for_context", new=AsyncMock(return_value=root_identity)),
            patch("cptr.routers.coding.unrestricted_root_identity", return_value=root_identity) as root_probe,
            patch("cptr.routers.coding.local_root_grant_store.grant", new=AsyncMock(return_value={})) as grant,
            patch("cptr.routers.coding.local_root_grant_store.is_active", new=AsyncMock(return_value=False)),
            patch("cptr.routers.coding.run_command", new=AsyncMock(return_value="Task deadbeef: exited (code 0)")) as run,
            patch("cptr.routers.coding._command_snapshot", new=AsyncMock(return_value=self._snapshot())),
            patch("cptr.routers.coding._touch_worker", new=AsyncMock(return_value=None)),
        ):
            result = await start_workspace_command(request, "ws-1", body)

        self.assertEqual(result["status"], "COMPLETE")
        root_probe.assert_called_once_with(root_identity)
        grant.assert_awaited_once_with(
            owner_id="user-1",
            session_id="wbs_1234567890abcdef",
            ttl_seconds=None,
        )
        run.assert_awaited_once()
        args, kwargs = run.await_args
        self.assertEqual(args[0], "rm -rf build")
        self.assertTrue(kwargs["__context__"]["local_root_unrestricted"])
        self.assertEqual(
            kwargs["__context__"]["root_workbench_session_id"],
            "wbs_1234567890abcdef",
        )

    async def test_active_session_grant_applies_to_later_local_root_commands_without_marker(self):
        request = self._request()
        workspace = SimpleNamespace(path="/tmp/cptr-root-workspace")
        body = CommandRequest(
            command="rm -rf build",
            workbench_session_id="wbs_1234567890abcdef",
        )
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.local_root_grant_store.is_active", new=AsyncMock(return_value=True)),
            patch("cptr.routers.coding.run_command", new=AsyncMock(return_value="Task deadbeef: exited (code 0)")) as run,
            patch("cptr.routers.coding._command_snapshot", new=AsyncMock(return_value=self._snapshot())),
            patch("cptr.routers.coding._touch_worker", new=AsyncMock(return_value=None)),
        ):
            await start_workspace_command(request, "ws-1", body)

        self.assertTrue(run.await_args.kwargs["__context__"]["local_root_unrestricted"])

    async def test_active_root_grant_still_requires_external_scope_for_network_commands(self):
        request = self._request()
        workspace = SimpleNamespace(path="/tmp/cptr-root-workspace")
        body = CommandRequest(
            command="npm install example-package",
            workbench_session_id="wbs_1234567890abcdef",
            allow_network=True,
        )
        run = AsyncMock(return_value="Task deadbeef: exited (code 0)")
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.local_root_grant_store.is_active", new=AsyncMock(return_value=True)),
            patch("cptr.routers.coding.run_command", new=run),
            self.assertRaises(HTTPException) as denied,
        ):
            await start_workspace_command(request, "ws-1", body)
        self.assertEqual(denied.exception.status_code, 403)
        run.assert_not_awaited()

    async def test_rejected_grant_command_does_not_persist_root_authority(self):
        request = self._request()
        workspace = SimpleNamespace(path="/tmp/cptr-root-workspace")
        body = CommandRequest(
            command=f"{ROOT_GRANT_MARKER}\nnpm install example-package",
            workbench_session_id="wbs_1234567890abcdef",
            allow_network=True,
        )
        grant = AsyncMock(return_value={})
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.local_root_grant_store.is_active", new=AsyncMock(return_value=False)),
            patch("cptr.routers.coding.local_root_grant_store.grant", new=grant),
            self.assertRaises(HTTPException) as denied,
        ):
            await start_workspace_command(request, "ws-1", body)
        self.assertEqual(denied.exception.status_code, 403)
        grant.assert_not_awaited()

    async def test_grant_marker_requires_workbench_session(self):
        request = self._request()
        workspace = SimpleNamespace(path="/tmp/cptr-root-workspace")
        body = CommandRequest(command=f"{ROOT_GRANT_MARKER}\nprintf ok")
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            self.assertRaises(HTTPException) as denied,
        ):
            await start_workspace_command(request, "ws-1", body)
        self.assertEqual(denied.exception.status_code, 403)

    async def test_unavailable_host_root_fails_before_persisting_grant(self):
        request = self._request()
        workspace = SimpleNamespace(path="/tmp/cptr-root-workspace")
        identity = SimpleNamespace(app_user_id="user-1", is_pam=True, uid=1000)
        body = CommandRequest(
            command=f"{ROOT_GRANT_MARKER}\nprintf ok",
            workbench_session_id="wbs_1234567890abcdef",
        )
        grant = AsyncMock(return_value={})
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.identity_for_context", new=AsyncMock(return_value=identity)),
            patch(
                "cptr.routers.coding.unrestricted_root_identity",
                side_effect=IdentityUnavailable("root unavailable"),
            ),
            patch("cptr.routers.coding.local_root_grant_store.grant", new=grant),
            patch("cptr.routers.coding.local_root_grant_store.is_active", new=AsyncMock(return_value=False)),
            self.assertRaises(HTTPException) as denied,
        ):
            await start_workspace_command(request, "ws-1", body)
        self.assertEqual(denied.exception.status_code, 409)
        grant.assert_not_awaited()

    async def test_revoke_marker_disables_root_before_following_command(self):
        request = self._request()
        workspace = SimpleNamespace(path="/tmp/cptr-root-workspace")
        body = CommandRequest(
            command=f"{ROOT_REVOKE_MARKER}\nprintf user-mode",
            workbench_session_id="wbs_1234567890abcdef",
        )
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.local_root_grant_store.revoke", new=AsyncMock(return_value=1)) as revoke,
            patch("cptr.routers.coding.local_root_grant_store.is_active", new=AsyncMock(return_value=False)),
            patch("cptr.routers.coding.run_command", new=AsyncMock(return_value="Task deadbeef: exited (code 0)")) as run,
            patch("cptr.routers.coding._command_snapshot", new=AsyncMock(return_value=self._snapshot())),
            patch("cptr.routers.coding._touch_worker", new=AsyncMock(return_value=None)),
        ):
            await start_workspace_command(request, "ws-1", body)

        revoke.assert_awaited_once_with(
            owner_id="user-1", session_id="wbs_1234567890abcdef"
        )
        self.assertNotIn("local_root_unrestricted", run.await_args.kwargs["__context__"])
        self.assertEqual(run.await_args.args[0], "printf user-mode")


if __name__ == "__main__":
    unittest.main()
