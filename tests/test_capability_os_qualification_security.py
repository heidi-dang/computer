"""Security regressions discovered during the independent qualification audit."""
import os
from dataclasses import replace
import unittest
from unittest.mock import patch
from tests import test_capability_os_gvisor_dispatcher as fixtures
from cptr.services.capability_os.sandbox_daemon import SandboxRuntimeUnavailable


class QualificationSecurityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CapabilityOsGvisorDispatcherTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_rootfs_digest_is_measured_not_just_copied_to_attestation(self):
        f = self.fixture
        with self.assertRaisesRegex(SandboxRuntimeUnavailable, "digest"):
            f.dispatcher._validate_rootfs(replace(f.identity, digest="sha256:" + "a" * 64))

    def test_writable_runtime_binary_is_rejected_before_execution(self):
        f = self.fixture
        os.chmod(f.runsc, 0o777)
        with self.assertRaises(SandboxRuntimeUnavailable):
            f.dispatcher._validate_runsc()

    def test_runsc_version_is_revalidated_after_configuration(self):
        f = self.fixture
        f.runsc.write_text("#!/bin/sh\nprintf 'runsc version changed\\n'\n")
        with self.assertRaisesRegex(SandboxRuntimeUnavailable, "release|version"):
            f.dispatcher._validate_runsc()

    def test_descendant_rootfs_permissions_are_checked(self):
        f = self.fixture
        os.chmod(f.rootfs / "usr/bin/python3", 0o777)
        with self.assertRaises(SandboxRuntimeUnavailable):
            f.dispatcher._validate_rootfs(f.identity)

    def test_descendant_rootfs_symlink_is_rejected(self):
        f = self.fixture
        (f.rootfs / "usr/lib").symlink_to("/usr/lib")
        with self.assertRaises(SandboxRuntimeUnavailable):
            f.dispatcher._validate_rootfs(f.identity)

    def test_python_version_is_measured_inside_sandbox_before_build(self):
        from cptr.services.capability_os.gvisor_dispatcher import BoundedProcessResult
        f = self.fixture
        with patch("cptr.services.capability_os.gvisor_dispatcher._run_bounded",
                   return_value=BoundedProcessResult(0, b"Python wrong\n", b"")):
            with self.assertRaisesRegex(SandboxRuntimeUnavailable, "Python.*version|runtime.*version"):
                f.dispatcher(f.request, f.staged)

    def test_staged_source_is_readable_to_unprivileged_sandbox(self):
        import stat
        from tests import test_capability_os_sandbox_daemon as daemon_fixtures
        f = daemon_fixtures.CapabilityOsSandboxDaemonTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        staged = f.store.stage(f.request())
        self.addCleanup(staged.cleanup)
        self.assertEqual(stat.S_IMODE(staged.source_dir.parent.stat().st_mode), 0o710)
        self.assertEqual(staged.source_dir.parent.stat().st_gid, f.store.expected_gid)
        self.assertEqual(stat.S_IMODE(staged.source_dir.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((staged.source_dir / "main.py").stat().st_mode), 0o444)
