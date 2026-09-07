"""Opt-in effect-level qualification for the live gVisor Capability OS boundary.

Normal CI keeps this test skipped because it requires the qualified runsc/rootfs
and broker socket. A production qualification host enables it with
CPTR_GVISOR_INTEGRATION=1. The assertions intentionally test effects rather
than command-string classification.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import textwrap
import unittest

from cptr.services.capability_os.sandbox_broker import (
    BrokerProtocolError,
    SandboxBrokerClient,
    SandboxRequest,
)


_ENABLED = os.environ.get("CPTR_GVISOR_INTEGRATION", "").lower() in {"1", "true", "yes"}
_SOCKET = Path(os.environ.get("CPTR_GVISOR_TEST_SOCKET", "/run/cptr-sandbox-qual.sock"))
_SPOOL = Path(
    os.environ.get(
        "CPTR_GVISOR_TEST_SPOOL",
        "/var/lib/cptr/data/capability-os/blobs",
    )
)


@unittest.skipUnless(_ENABLED, "requires the qualified live gVisor broker")
class CapabilityOsGvisorEffectTests(unittest.TestCase):
    def _bundle(self, source: str) -> str:
        raw = json.dumps(
            {"files": {"main.py": source}},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        path = _SPOOL / "sha256" / f"{digest.split(':', 1)[1]}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o600)
        return digest

    def _request(self, *, digest: str, profile: str = "python") -> SandboxRequest:
        return SandboxRequest(
            operation="run",
            task_id="qualification-effects",
            lease_id="qualification-effects-lease",
            artifact_digest=digest,
            runtime_class="gvisor",
            bundle_digest=digest,
            profile=profile,
            entrypoint="main.py",
            timeout_ms=15_000,
            resources={
                "cpuMillis": 5_000,
                "memoryMiB": 128,
                "diskMiB": 32,
                "pids": 16,
                "wallTimeMs": 15_000,
                "maxOutputBytes": 65_536,
            },
            network={"outbound": "deny", "destinations": []},
        )

    def test_absolute_interpreter_subprocess_filesystem_network_and_secrets_are_confined(self):
        source = textwrap.dedent(
            """
            import json
            import os
            import pathlib
            import socket
            import subprocess

            checks = {}
            checks["service_env_hidden"] = (
                "CPTR_SANDBOX_PEER_USER" not in os.environ
                and "CPTR_SANDBOX_SPOOL_ROOT" not in os.environ
            )
            host_spool = pathlib.Path("/var/lib/cptr/data/capability-os/blobs")
            checks["host_spool_hidden"] = not host_spool.exists()

            try:
                pathlib.Path("/work/write-probe").write_text("escape")
                checks["work_read_only"] = False
            except Exception:
                checks["work_read_only"] = True

            try:
                cap_eff = next(
                    line.split(":", 1)[1].strip()
                    for line in pathlib.Path("/proc/self/status").read_text().splitlines()
                    if line.startswith("CapEff:")
                )
                checks["guest_caps_empty"] = int(cap_eff, 16) == 0
            except Exception:
                checks["guest_caps_empty"] = False

            try:
                child = subprocess.run(
                    [
                        "/usr/bin/python3",
                        "-I",
                        "-c",
                        "import pathlib; print(pathlib.Path('/var/lib/cptr/data/capability-os/blobs').exists())",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=True,
                )
                checks["absolute_interpreter_confined"] = child.stdout.strip() == "False"
            except Exception:
                checks["absolute_interpreter_confined"] = False

            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                sock.connect(("1.1.1.1", 53))
                checks["egress_denied"] = False
            except Exception:
                checks["egress_denied"] = True
            finally:
                if sock is not None:
                    sock.close()

            checks["all"] = all(checks.values())
            print(json.dumps(checks, sort_keys=True))
            """
        )
        digest = self._bundle(source)
        result = asyncio.run(
            SandboxBrokerClient(socket_path=_SOCKET).invoke(self._request(digest=digest))
        )
        self.assertIsNotNone(result.get("output"))
        output = dict(result["output"])
        self.assertTrue(output.pop("all"))
        self.assertTrue(all(output.values()), output)
        attestation = dict(result.get("attestation") or {})
        self.assertTrue(attestation.get("rootless"))
        self.assertEqual(attestation.get("hostRuntimeUser"), "cptr")
        self.assertEqual(attestation.get("network"), "deny")

    def test_unqualified_shell_profile_cannot_be_used_as_package_script_escape(self):
        digest = self._bundle("print('unreachable')\n")
        with self.assertRaises(BrokerProtocolError):
            asyncio.run(
                SandboxBrokerClient(socket_path=_SOCKET).invoke(
                    self._request(digest=digest, profile="shell")
                )
            )


if __name__ == "__main__":
    unittest.main()
