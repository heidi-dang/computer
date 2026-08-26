import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from cptr.routers.coding import (
    SshCommandRequest,
    _parse_ssh_aliases,
    _ssh_runtime,
    _validate_command,
    start_ssh_command,
)


class SshControlTests(unittest.IsolatedAsyncioTestCase):
    def test_alias_parser_exposes_only_literal_host_aliases(self):
        config = """
        Host aws prod.example
          HostName 203.0.113.10
          IdentityFile ~/.ssh/id_ed25519
        Host *.internal !blocked.internal
          User deploy
        Host staging # comment
          HostName staging.example
        """
        self.assertEqual(_parse_ssh_aliases(config), ["aws", "prod.example", "staging"])

    def test_generic_direct_command_rejects_ssh_even_with_network_approval(self):
        for command in ("ssh aws", "scp file aws:/tmp/file", "rsync file aws:/tmp/file"):
            with self.subTest(command=command), self.assertRaises(HTTPException) as denied:
                _validate_command(command, True)
            self.assertEqual(denied.exception.status_code, 403)
            self.assertIn("dedicated SSH", str(denied.exception.detail))

    async def test_start_ssh_command_uses_argv_and_preserves_host_key_defaults(self):
        request = SimpleNamespace(state=SimpleNamespace(control_scopes={"command:external"}))
        workspace = SimpleNamespace(path="/tmp/cptr-ssh-workspace")
        session = {"workspace": workspace.path}
        body = SshCommandRequest(alias="AWS", command="uname -a && id", wait_seconds=5)
        snapshot = {
            "command_id": "deadbeef",
            "status": "COMPLETE",
            "exit_code": 0,
            "output": "ok",
            "next_offset": 2,
        }
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding._ssh_runtime",
                new=AsyncMock(return_value=("/usr/bin/ssh", ["aws"])),
            ),
            patch(
                "cptr.routers.coding.run_command",
                new=AsyncMock(return_value="Task deadbeef: exited (code 0)"),
            ) as run,
            patch("cptr.routers.coding.get_command_session", return_value=session),
            patch("cptr.routers.coding._command_snapshot", new=AsyncMock(return_value=snapshot)),
        ):
            result = await start_ssh_command(request, "ws_1", body)

        self.assertEqual(result["alias"], "aws")
        self.assertEqual(session["transport"], "ssh")
        self.assertEqual(session["ssh_alias"], "aws")
        run.assert_awaited_once()
        args, kwargs = run.await_args
        self.assertEqual(args[:3], ("ssh aws", ".", 5))
        argv = kwargs["__argv"]
        self.assertEqual(argv[0], "/usr/bin/ssh")
        self.assertEqual(argv[-2:], ["aws", "uname -a && id"])
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=15", argv)
        self.assertFalse(any("StrictHostKeyChecking" in value for value in argv))
        self.assertFalse(any("UserKnownHostsFile" in value for value in argv))

    async def test_start_ssh_command_rejects_unconfigured_alias(self):
        request = SimpleNamespace(state=SimpleNamespace(control_scopes={"command:external"}))
        workspace = SimpleNamespace(path="/tmp/cptr-ssh-workspace")
        body = SshCommandRequest(alias="unknown", command="true", wait_seconds=0)
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding._ssh_runtime",
                new=AsyncMock(return_value=("/usr/bin/ssh", ["aws"])),
            ),
            self.assertRaises(HTTPException) as denied,
        ):
            await start_ssh_command(request, "ws_1", body)
        self.assertEqual(denied.exception.status_code, 422)
        self.assertEqual(denied.exception.detail, "SSH alias is not configured")

    async def test_ssh_runtime_fails_closed_when_client_missing(self):
        request = SimpleNamespace()
        identity = SimpleNamespace(home="/home/test", is_pam=False, app_user_id="user_1")
        with (
            patch("cptr.routers.coding.identity_for_context", new=AsyncMock(return_value=identity)),
            patch("cptr.routers.coding.env_for", return_value={"PATH": "/missing"}),
            patch("cptr.routers.coding.shutil.which", return_value=None),
            self.assertRaises(HTTPException) as unavailable,
        ):
            await _ssh_runtime(request, user_id="user_1", workspace_path="/tmp/workspace")
        self.assertEqual(unavailable.exception.status_code, 503)
        self.assertEqual(unavailable.exception.detail, "OpenSSH client is not available")

    async def test_ssh_requires_external_command_scope(self):
        request = SimpleNamespace(state=SimpleNamespace(control_scopes=set()))
        body = SshCommandRequest(alias="aws", command="true", wait_seconds=0)
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            self.assertRaises(HTTPException) as denied,
        ):
            await start_ssh_command(request, "ws_1", body)
        self.assertEqual(denied.exception.status_code, 403)
        self.assertIn("command:external", denied.exception.detail)


if __name__ == "__main__":
    unittest.main()
