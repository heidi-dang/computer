import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cptr.services.capability_os.sandbox_broker import (
    BrokerProtocolError,
    SandboxBrokerClient,
    SandboxRequest,
)


class CapabilityOsSandboxBrokerTests(unittest.IsolatedAsyncioTestCase):
    def test_request_rejects_shell_passthrough_and_non_digest_identity(self):
        with self.assertRaises(BrokerProtocolError):
            SandboxRequest(
                operation="run",
                task_id="task-1",
                lease_id="lease-1",
                artifact_digest="not-a-digest",
                runtime_class="gvisor",
                bundle_digest="sha256:" + "2" * 64,
                profile="python",
            )
        with self.assertRaises(TypeError):
            SandboxRequest(
                operation="run",
                task_id="task-1",
                lease_id="lease-1",
                artifact_digest="sha256:" + "1" * 64,
                runtime_class="gvisor",
                bundle_digest="sha256:" + "2" * 64,
                profile="python",
                command=["/bin/sh"],
            )

    def test_client_resolves_default_group_only_at_socket_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broker.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            os.chmod(path, 0o660)
            try:
                st = path.stat()
                with patch(
                    "cptr.services.capability_os.sandbox_broker.grp.getgrnam",
                    side_effect=KeyError("cptr"),
                ):
                    client = SandboxBrokerClient(socket_path=path, expected_uid=st.st_uid)
                    self.assertIsNone(client.expected_gid)
                    with self.assertRaisesRegex(BrokerProtocolError, "required cptr group does not exist"):
                        client._verify_socket()
            finally:
                server.close()

    async def test_client_verifies_socket_metadata_and_round_trips_bounded_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broker.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            os.chmod(path, 0o660)

            async def serve_once():
                conn, _ = await asyncio.to_thread(server.accept)
                try:
                    header = await asyncio.to_thread(conn.recv, 4)
                    length = int.from_bytes(header, "big")
                    body = b""
                    while len(body) < length:
                        body += await asyncio.to_thread(conn.recv, length - len(body))
                    request = json.loads(body)
                    response = json.dumps({"ok": True, "requestId": request["requestId"], "result": {"status": "accepted"}}).encode()
                    await asyncio.to_thread(conn.sendall, len(response).to_bytes(4, "big") + response)
                finally:
                    conn.close()
                    server.close()

            task = asyncio.create_task(serve_once())
            st = path.stat()
            client = SandboxBrokerClient(
                socket_path=path,
                expected_uid=st.st_uid,
                expected_gid=st.st_gid,
                max_frame_bytes=8192,
            )
            result = await client.invoke(SandboxRequest(
                operation="run",
                task_id="task-1",
                lease_id="lease-1",
                artifact_digest="sha256:" + "1" * 64,
                runtime_class="gvisor",
                bundle_digest="sha256:" + "2" * 64,
                profile="python",
            ))
            self.assertEqual(result["status"], "accepted")
            await task

    async def test_client_uses_request_deadline_after_fast_connect(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broker.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            os.chmod(path, 0o660)

            async def serve_once():
                conn, _ = await asyncio.to_thread(server.accept)
                try:
                    header = await asyncio.to_thread(conn.recv, 4)
                    length = int.from_bytes(header, "big")
                    body = b""
                    while len(body) < length:
                        body += await asyncio.to_thread(conn.recv, length - len(body))
                    request = json.loads(body)
                    await asyncio.sleep(0.2)
                    response = json.dumps({
                        "ok": True,
                        "requestId": request["requestId"],
                        "result": {"status": "delayed-ok"},
                    }).encode()
                    await asyncio.to_thread(conn.sendall, len(response).to_bytes(4, "big") + response)
                finally:
                    conn.close()
                    server.close()

            task = asyncio.create_task(serve_once())
            st = path.stat()
            client = SandboxBrokerClient(
                socket_path=path,
                expected_uid=st.st_uid,
                expected_gid=st.st_gid,
                connect_timeout_seconds=0.1,
            )
            result = await client.invoke(SandboxRequest(
                operation="run",
                task_id="task-1",
                lease_id="lease-1",
                artifact_digest="sha256:" + "1" * 64,
                runtime_class="gvisor",
                bundle_digest="sha256:" + "2" * 64,
                profile="python",
                timeout_ms=100,
            ))
            self.assertEqual(result["status"], "delayed-ok")
            await task

    async def test_client_normalizes_early_peer_disconnect_to_protocol_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broker.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            os.chmod(path, 0o660)

            async def reject_once():
                conn, _ = await asyncio.to_thread(server.accept)
                try:
                    conn.close()
                finally:
                    server.close()

            task = asyncio.create_task(reject_once())
            st = path.stat()
            client = SandboxBrokerClient(
                socket_path=path,
                expected_uid=st.st_uid,
                expected_gid=st.st_gid,
            )
            with self.assertRaisesRegex(BrokerProtocolError, "rejected peer connection"):
                await client.invoke(SandboxRequest(
                    operation="status",
                    task_id="task-1",
                    lease_id="lease-1",
                    artifact_digest="sha256:" + "1" * 64,
                    runtime_class="gvisor",
                    bundle_digest="sha256:" + "2" * 64,
                    profile="python",
                ))
            await task

    async def test_client_rejects_non_socket_or_wrong_mode_before_connect(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broker.sock"
            path.write_text("not a socket")
            client = SandboxBrokerClient(socket_path=path, expected_uid=os.getuid(), expected_gid=os.getgid())
            with self.assertRaises(BrokerProtocolError):
                await client.invoke(SandboxRequest(
                    operation="status",
                    task_id="task-1",
                    lease_id="lease-1",
                    artifact_digest="sha256:" + "1" * 64,
                    runtime_class="gvisor",
                    bundle_digest="sha256:" + "2" * 64,
                    profile="python",
                ))

            path.unlink()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            os.chmod(path, 0o666)
            try:
                client = SandboxBrokerClient(socket_path=path, expected_uid=path.stat().st_uid, expected_gid=path.stat().st_gid)
                with self.assertRaises(BrokerProtocolError):
                    await client.invoke(SandboxRequest(
                        operation="status",
                        task_id="task-1",
                        lease_id="lease-1",
                        artifact_digest="sha256:" + "1" * 64,
                        runtime_class="gvisor",
                        bundle_digest="sha256:" + "2" * 64,
                        profile="python",
                    ))
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()
