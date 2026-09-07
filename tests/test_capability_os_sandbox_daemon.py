import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from cptr.services.capability_os.sandbox_broker import BrokerProtocolError, SandboxRequest
from cptr.services.capability_os.sandbox_daemon import (
    BundleStore,
    SandboxBrokerEngine,
    SandboxRuntimeUnavailable,
)


def _bundle(root: Path, files: dict[str, str]):
    raw = json.dumps({"files": files}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    path = root / "sha256" / f"{digest.split(chr(58), 1)[1]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return digest, path


class CapabilityOsSandboxDaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.spool = root / "spool"
        self.state = root / "state"
        self.digest, self.path = _bundle(self.spool, {"main.py": "print(ok)\n"})
        self.store = BundleStore(
            spool_root=self.spool,
            state_root=self.state,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, **overrides):
        data = dict(
            operation="run",
            task_id="task-1",
            lease_id="lease-1",
            artifact_digest="sha256:" + "1" * 64,
            runtime_class="gvisor",
            bundle_digest=self.digest,
            profile="python",
            entrypoint="main.py",
        )
        data.update(overrides)
        return SandboxRequest(**data)

    def test_bundle_is_reverified_and_copied_to_private_staging(self):
        staged = self.store.stage(self.request())
        try:
            self.assertEqual((staged.source_dir / "main.py").read_text(), "print(ok)\n")
            self.assertTrue(str(staged.source_dir).startswith(str(self.state)))
            self.assertNotEqual(staged.source_dir, self.path.parent)
            self.assertEqual(staged.bundle_digest, self.digest)
        finally:
            staged.cleanup()

    def test_tamper_missing_entrypoint_and_symlink_fail_closed(self):
        self.path.write_text("{\"files\":{\"main.py\":\"tampered\"}}")
        with self.assertRaises(BrokerProtocolError):
            self.store.stage(self.request())

        self.digest, self.path = _bundle(self.spool, {"other.py": "pass\n"})
        with self.assertRaises(BrokerProtocolError):
            self.store.stage(self.request(bundle_digest=self.digest))

        self.path.unlink()
        target = self.spool / "target.json"
        target.write_text("{}")
        self.path.symlink_to(target)
        with self.assertRaises(BrokerProtocolError):
            self.store.stage(self.request(bundle_digest=self.digest))

    def test_engine_status_is_truthful_and_run_fails_without_runtime(self):
        engine = SandboxBrokerEngine(
            bundles=self.store,
            runsc_path=Path(self.tmp.name) / "missing-runsc",
            egress_proxy_available=False,
        )
        status = engine.handle(self.request(operation="status"))
        self.assertFalse(status["runtimes"]["gvisor"])
        with self.assertRaises(SandboxRuntimeUnavailable):
            engine.handle(self.request())

    def test_binary_presence_is_not_runtime_readiness_without_qualified_dispatcher(self):
        fake_runsc = Path(self.tmp.name) / "runsc"
        fake_runsc.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(fake_runsc, 0o755)
        engine = SandboxBrokerEngine(
            bundles=self.store,
            runsc_path=fake_runsc,
            egress_proxy_available=False,
        )
        status = engine.handle(self.request(operation="status"))
        self.assertTrue(status["installedRuntimes"]["gvisor"])
        self.assertFalse(status["runtimes"]["gvisor"])
        with self.assertRaises(SandboxRuntimeUnavailable):
            engine.handle(self.request())

    def test_allow_list_network_fails_without_egress_enforcement(self):
        fake_runsc = Path(self.tmp.name) / "runsc"
        fake_runsc.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(fake_runsc, 0o755)
        engine = SandboxBrokerEngine(
            bundles=self.store,
            runsc_path=fake_runsc,
            egress_proxy_available=False,
        )
        with self.assertRaises(SandboxRuntimeUnavailable):
            engine.handle(self.request(network={"outbound": "allow-list", "destinations": ["api.example.com"]}))

    def test_peer_identity_must_match_configured_cptr_identity(self):
        engine = SandboxBrokerEngine(
            bundles=self.store,
            runsc_path=Path(self.tmp.name) / "missing",
            expected_peer_uid=1002,
            expected_peer_gid=1003,
        )
        self.assertTrue(engine.peer_allowed(uid=1002, gid=1003))
        self.assertFalse(engine.peer_allowed(uid=0, gid=0))
        self.assertFalse(engine.peer_allowed(uid=1002, gid=9999))


if __name__ == "__main__":
    unittest.main()
