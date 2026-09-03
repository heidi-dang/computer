import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.routers.coding import CommandRequest, _command_snapshot, start_workspace_command
from cptr.services.live_events import LiveEventHub, LiveEventStore, command_target_key
from cptr.services.lsp_manager import LspManager
from cptr.utils.tools import command_sessions, run_command


class TerminalParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_pty_live_events_preserve_stdout_and_stderr_stream_identity(self):
        hub = LiveEventHub(store=LiveEventStore())
        identity = SimpleNamespace(is_pam=False, app_user_id="user_1")
        request = SimpleNamespace()
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context", new=AsyncMock(return_value=identity)
                ),
                patch("cptr.utils.tools.Runtime.write_file", new=AsyncMock(return_value={})),
                patch("cptr.services.live_events.live_event_hub", hub),
            ):
                result = await run_command(
                    f"{sys.executable} -c \"import sys; print('out'); print('err', file=sys.stderr)\"",
                    ".",
                    5,
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
            chunks = [event.payload for event in events if event.event_type == "terminal.chunk"]
            streams = {str(chunk.get("stream")) for chunk in chunks}
            self.assertIn("stdout", streams)
            self.assertIn("stderr", streams)
            self.assertIn(
                "out",
                "".join(
                    str(chunk.get("text") or "")
                    for chunk in chunks
                    if chunk.get("stream") == "stdout"
                ),
            )
            self.assertIn(
                "err",
                "".join(
                    str(chunk.get("text") or "")
                    for chunk in chunks
                    if chunk.get("stream") == "stderr"
                ),
            )
        finally:
            command_sessions.pop(command_id, None)

    async def test_direct_command_can_opt_into_pty_dimensions_and_initial_stdin(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-direct-coding")
        body = CommandRequest(
            command="cat", pty=True, rows=40, cols=132, stdin="hello\n", wait_seconds=0
        )
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
                        "pty": True,
                        "rows": 40,
                        "cols": 132,
                        "recovered": False,
                    }
                ),
            ),
        ):
            result = await start_workspace_command(request, "ws_1", body)
        self.assertTrue(result["pty"])
        kwargs = run.await_args.kwargs
        self.assertTrue(kwargs["__use_pty"])
        self.assertEqual(kwargs["__rows"], 40)
        self.assertEqual(kwargs["__cols"], 132)
        self.assertEqual(kwargs["__stdin"], "hello\n")

    async def test_recovered_command_snapshot_streams_rotated_log_with_global_offsets(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            log_dir = Path(workspace_root, ".cptr", "task_logs")
            log_dir.mkdir(parents=True)
            log_path = log_dir / "feedface.jsonl"
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "log_rotated", "ts": 20.0}),
                        json.dumps(
                            {
                                "type": "output",
                                "stream": "stdout",
                                "data": "alpha-",
                                "offset_end": 100006,
                                "ts": 20.1,
                            }
                        ),
                        json.dumps(
                            {
                                "type": "output",
                                "stream": "stdout",
                                "data": "omega",
                                "offset_end": 100011,
                                "ts": 20.2,
                            }
                        ),
                        json.dumps(
                            {
                                "type": "end",
                                "exit_code": 0,
                                "total_bytes": 100011,
                                "started_at": 10.0,
                                "ts": 20.3,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.object(
                Path, "read_text", side_effect=AssertionError("whole-file read used")
            ):
                snapshot = await _command_snapshot(
                    SimpleNamespace(),
                    workspace_path=workspace_root,
                    command_id="feedface",
                    offset=100006,
                )

        self.assertEqual(snapshot["status"], "COMPLETE")
        self.assertEqual(snapshot["next_offset"], 100011)
        self.assertEqual(snapshot["output"], "omega")
        self.assertGreaterEqual(snapshot["duration_ms"], 10_000)
        self.assertTrue(snapshot["recovered"])

    async def test_completed_command_snapshot_recovers_from_durable_jsonl_after_registry_loss(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            log_dir = Path(workspace_root, ".cptr", "task_logs")
            log_dir.mkdir(parents=True)
            (log_dir / "deadbeef.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "start",
                                "command": "printf recovered",
                                "pid": 99,
                                "ts": 10.0,
                                "pty": False,
                                "rows": 24,
                                "cols": 80,
                            }
                        ),
                        json.dumps(
                            {"type": "output", "stream": "stdout", "data": "recovered", "ts": 10.1}
                        ),
                        json.dumps({"type": "end", "exit_code": 0, "ts": 10.2}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = await _command_snapshot(
                SimpleNamespace(), workspace_path=workspace_root, command_id="deadbeef"
            )
        self.assertEqual(snapshot["status"], "COMPLETE")
        self.assertEqual(snapshot["exit_code"], 0)
        self.assertEqual(snapshot["output"], "recovered")
        self.assertTrue(snapshot["recovered"])


class LspManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_language_server_round_trip_and_lifecycle(self):
        source = textwrap.dedent(r"""
            import json, sys

            def read_message():
                headers = {}
                while True:
                    line = sys.stdin.buffer.readline()
                    if not line:
                        raise SystemExit(0)
                    if line in (b"\r\n", b"\n"):
                        break
                    key, value = line.decode().split(":", 1)
                    headers[key.lower()] = value.strip()
                body = sys.stdin.buffer.read(int(headers.get("content-length", "0")))
                return json.loads(body)

            def send_message(message):
                payload = json.dumps(message).encode()
                sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
                sys.stdout.buffer.flush()

            while True:
                msg = read_message()
                if "id" not in msg:
                    continue
                if msg.get("method") == "initialize":
                    send_message({
                        "jsonrpc": "2.0",
                        "id": "server-configuration",
                        "method": "workspace/configuration",
                        "params": {"items": [{"section": "fake"}]},
                    })
                    response = read_message()
                    if response.get("id") != "server-configuration" or response.get("result") != [None]:
                        raise SystemExit(3)
                send_message({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"echo_method": msg.get("method"), "capabilities": {}},
                })
        """)
        with tempfile.TemporaryDirectory() as root:
            script = Path(root, "fake_lsp.py")
            script.write_text(source, encoding="utf-8")
            manager = LspManager(server_commands={"fake": [sys.executable, str(script)]})
            started = await manager.start(server_id="fake", root=Path(root), user_id="user_1")
            try:
                reply = await manager.request(
                    lsp_id=started["lsp_id"],
                    user_id="user_1",
                    method="textDocument/hover",
                    params={
                        "textDocument": {"uri": script.as_uri()},
                        "position": {"line": 0, "character": 0},
                    },
                )
                self.assertEqual(reply["result"]["echo_method"], "textDocument/hover")
            finally:
                stopped = await manager.stop(lsp_id=started["lsp_id"], user_id="user_1")
                self.assertEqual(stopped["status"], "STOPPED")


if __name__ == "__main__":
    unittest.main()
