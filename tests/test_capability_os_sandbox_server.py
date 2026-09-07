import grp
import json
import os
import pwd
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cptr.services.capability_os.runtime_identity import measured_rootfs_digest
from cptr.services.capability_os.sandbox_broker import SandboxRequest
from cptr.services.capability_os.sandbox_daemon import BundleStore, SandboxBrokerEngine
from cptr.services.capability_os.sandbox_server import _gvisor_dispatcher_from_env, serve_connection


def _recv_exact(sock, size):
    out = b""
    while len(out) < size:
        chunk = sock.recv(size - len(out))
        if not chunk:
            raise RuntimeError("short frame")
        out += chunk
    return out


class CapabilityOsSandboxServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        bundles = BundleStore(spool_root=root / "spool", state_root=root / "state",
                              expected_uid=os.getuid(), expected_gid=os.getgid())
        self.engine = SandboxBrokerEngine(bundles=bundles, runsc_path=root / "missing",
                                          expected_peer_uid=os.getuid(), expected_peer_gid=os.getgid())

    def tearDown(self):
        self.tmp.cleanup()

    def _roundtrip(self, payload):
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        thread = threading.Thread(target=serve_connection, args=(server, self.engine), daemon=True)
        thread.start()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        client.sendall(len(raw).to_bytes(4, "big") + raw)
        length = int.from_bytes(_recv_exact(client, 4), "big")
        response = json.loads(_recv_exact(client, length))
        client.close()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        return response

    def test_status_roundtrip_uses_peer_credentials_and_bounded_protocol(self):
        req = SandboxRequest(operation="status", task_id="task-1", lease_id="lease-1",
                             artifact_digest="sha256:" + "1" * 64, runtime_class="gvisor",
                             bundle_digest="sha256:" + "2" * 64, profile="python")
        response = self._roundtrip(req.to_dict())
        self.assertTrue(response["ok"])
        self.assertEqual(response["requestId"], req.request_id)
        self.assertFalse(response["result"]["runtimes"]["gvisor"])

    def test_gvisor_dispatcher_requires_complete_explicit_qualified_rootfs_profile(self):
        root = Path(self.tmp.name)
        runsc = root / "runsc"
        runsc.write_text("#!/bin/sh\nprintf 'runsc version release-test\\nspec: 1.2.1\\n'\n", encoding="utf-8")
        os.chmod(runsc, 0o755)
        rootfs = root / "rootfs"
        python = rootfs / "usr" / "bin" / "python3"
        python.parent.mkdir(parents=True)
        python.write_text("fixture", encoding="utf-8")
        os.chmod(python, 0o755)
        state = root / "state-gvisor"

        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_gvisor_dispatcher_from_env(
                runsc_path=runsc, state_root=state, expected_rootfs_uid=os.getuid()
            ))

        partial = {"CPTR_SANDBOX_PYTHON_ROOTFS": str(rootfs)}
        with patch.dict(os.environ, partial, clear=True):
            with self.assertRaises(RuntimeError):
                _gvisor_dispatcher_from_env(
                    runsc_path=runsc, state_root=state, expected_rootfs_uid=os.getuid()
                )

        runtime_user = pwd.getpwuid(os.getuid()).pw_name
        runtime_group = grp.getgrgid(os.getgid()).gr_name
        configured = {
            "CPTR_SANDBOX_PYTHON_ROOTFS": str(rootfs),
            "CPTR_SANDBOX_PYTHON_ROOTFS_DIGEST": measured_rootfs_digest(rootfs, expected_uid=os.getuid()),
            "CPTR_SANDBOX_PYTHON_RUNTIME_VERSION": "Python fixture",
            "CPTR_SANDBOX_EXPECTED_RUNSC_VERSION": "release-test",
            "CPTR_SANDBOX_SYSTEMD_SLICE": "cptr-sandbox.slice",
            "CPTR_SANDBOX_RUNTIME_USER": runtime_user,
            "CPTR_SANDBOX_RUNTIME_GROUP": runtime_group,
        }
        with patch.dict(os.environ, configured, clear=True):
            dispatcher = _gvisor_dispatcher_from_env(
                runsc_path=runsc, state_root=state, expected_rootfs_uid=os.getuid()
            )
        self.assertIsNotNone(dispatcher)
        self.assertEqual(dispatcher.runsc_version, "release-test")
        self.assertEqual(dispatcher.systemd_slice, "cptr-sandbox.slice")
        self.assertEqual(dispatcher.runtime_user, runtime_user)
        self.assertEqual(dispatcher.runtime_group, runtime_group)
        self.assertEqual(dispatcher.runtime_uid, os.getuid())
        self.assertEqual(dispatcher.runtime_gid, os.getgid())
        self.assertEqual(dispatcher.rootfs["python"].path, rootfs)

        configured["CPTR_SANDBOX_EXPECTED_RUNSC_VERSION"] = "release-other"
        with patch.dict(os.environ, configured, clear=True):
            with self.assertRaises(RuntimeError):
                _gvisor_dispatcher_from_env(
                    runsc_path=runsc, state_root=state, expected_rootfs_uid=os.getuid()
                )

    def test_unknown_shell_field_is_rejected_without_execution(self):
        req = SandboxRequest(operation="status", task_id="task-1", lease_id="lease-1",
                             artifact_digest="sha256:" + "1" * 64, runtime_class="gvisor",
                             bundle_digest="sha256:" + "2" * 64, profile="python").to_dict()
        req["command"] = "id"
        response = self._roundtrip(req)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "BROKER_PROTOCOL_ERROR")


if __name__ == "__main__":
    unittest.main()
