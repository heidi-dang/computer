import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cptr.services.capability_os.runtime_identity import measured_rootfs_digest
from cptr.services.capability_os.gvisor_dispatcher import GvisorDispatcher, RootfsIdentity, BoundedProcessResult
from cptr.services.capability_os.sandbox_broker import SandboxRequest
from cptr.services.capability_os.sandbox_daemon import StagedBundle, SandboxRuntimeUnavailable


class CapabilityOsGvisorDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "main.py").write_text("print(1)\n", encoding="utf-8")
        self.rootfs = root / "rootfs"
        (self.rootfs / "usr" / "bin").mkdir(parents=True)
        for directory in (self.rootfs, self.rootfs / "usr", self.rootfs / "usr" / "bin"):
            os.chmod(directory, 0o755)
        (self.rootfs / "usr" / "bin" / "python3").write_text("fixture", encoding="utf-8")
        os.chmod(self.rootfs / "usr" / "bin" / "python3", 0o755)
        self.runsc = root / "runsc"
        self.runsc.write_text("#!/bin/sh\nprintf 'runsc version release-test\\n'\n", encoding="utf-8")
        os.chmod(self.runsc, 0o755)
        self.identity = RootfsIdentity(
            profile="python",
            path=self.rootfs,
            digest=measured_rootfs_digest(self.rootfs, expected_uid=os.getuid()),
            runtime_version="Python 3.14.4",
        )
        self.dispatcher = GvisorDispatcher(
            runsc_path=self.runsc,
            rootfs={"python": self.identity},
            state_root=root / "runtime",
            runsc_version="release-test",
            systemd_slice="cptr-sandbox.slice",
            runtime_user="cptr",
            runtime_group="cptr",
            runtime_uid=os.getuid(),
            runtime_gid=os.getgid(),
            expected_rootfs_uid=os.getuid(),
        )
        self.request = SandboxRequest(
            operation="build",
            task_id="task-1",
            lease_id="lease-1",
            artifact_digest="sha256:" + "1" * 64,
            runtime_class="gvisor",
            bundle_digest="sha256:" + "2" * 64,
            profile="python",
            entrypoint="main.py",
            timeout_ms=5000,
            resources={
                "cpuMillis": 1000,
                "memoryMiB": 128,
                "diskMiB": 64,
                "pids": 8,
                "wallTimeMs": 5000,
                "maxOutputBytes": 65536,
            },
            network={"outbound": "deny", "destinations": []},
        )
        self.staged = StagedBundle(source_dir=self.source, bundle_digest=self.request.bundle_digest)

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_has_read_only_root_and_source_empty_caps_unprivileged_user_and_limits(self):
        config = self.dispatcher.build_oci_config(self.request, self.staged)
        self.assertTrue(config["root"]["readonly"])
        self.assertEqual(config["root"]["path"], str(self.rootfs.resolve()))
        self.assertEqual(config["process"]["user"], {"uid": 0, "gid": 0})
        self.assertTrue(config["process"]["noNewPrivileges"])
        self.assertEqual(config["process"]["capabilities"]["effective"], [])
        work = next(m for m in config["mounts"] if m["destination"] == "/work")
        self.assertIn("ro", work["options"])
        self.assertEqual(Path(work["source"]), self.source.resolve())
        tmp = next(m for m in config["mounts"] if m["destination"] == "/tmp")
        self.assertIn("size=64m", tmp["options"])
        self.assertEqual(config["linux"]["resources"], {})
        self.assertEqual(config["process"]["args"], ["/usr/bin/python3", "-m", "py_compile", "/work/main.py"])
        self.assertIn("PYTHONHOME=/usr", config["process"]["env"])
        self.assertIn("CPTR_TOOL_INPUT_JSON={}", config["process"]["env"])
        argv, memory_max, tasks_max = self.dispatcher._transient_run_argv(
            unit_name="cptr-test", bundle=self.source.parent, runtime_root=self.source,
            container_id="cptr-test", limits=self.request.resources, timeout_seconds=5.0,
        )
        self.assertIn("--property=User=cptr", argv)
        self.assertIn("--property=Group=cptr", argv)
        self.assertIn("--property=CapabilityBoundingSet=", argv)
        self.assertIn("--rootless=true", argv)
        self.assertIn("--ignore-cgroups", argv)
        self.assertIn("--network=none", argv)
        self.assertEqual(memory_max, (128 + 192) * 1024 * 1024)
        self.assertEqual(tasks_max, 128)

    def test_qualified_python_interpreter_must_be_regular_and_executable(self):
        python = self.rootfs / "usr" / "bin" / "python3"
        os.chmod(python, 0o644)
        with self.assertRaises(SandboxRuntimeUnavailable):
            self.dispatcher.build_oci_config(self.request, self.staged)
        python.unlink()
        target = self.rootfs / "usr" / "bin" / "python-real"
        target.write_text("fixture", encoding="utf-8")
        os.chmod(target, 0o755)
        python.symlink_to(target)
        with self.assertRaises(SandboxRuntimeUnavailable):
            self.dispatcher.build_oci_config(self.request, self.staged)

    def test_allow_list_and_missing_required_resource_limits_fail_closed(self):
        allow = SandboxRequest(**{**self.request.__dict__, "network": {"outbound": "allow-list", "destinations": ["api.example.com"]}})
        with self.assertRaises(SandboxRuntimeUnavailable):
            self.dispatcher.build_oci_config(allow, self.staged)
        missing = SandboxRequest(**{**self.request.__dict__, "resources": {"memoryMiB": 128}})
        with self.assertRaises(SandboxRuntimeUnavailable):
            self.dispatcher.build_oci_config(missing, self.staged)

    def test_success_attestation_binds_source_rootfs_runtime_and_output(self):
        class Completed:
            returncode = 0
            stdout = b"compiled\n"
            stderr = b""
        with patch.object(self.dispatcher, "_cleanup"), patch.object(
            self.dispatcher, "_write_config", wraps=self.dispatcher._write_config
        ) as write_config, patch(
            "cptr.services.capability_os.gvisor_dispatcher._run_bounded",
            side_effect=[BoundedProcessResult(0, b"Python 3.14.4\n", b""), Completed()],
        ) as run:
            result = self.dispatcher(self.request, self.staged)
        probe_args = write_config.call_args_list[0].args[1]["process"]["args"]
        self.assertEqual(probe_args[:2], ["/usr/bin/python3", "-c"])
        self.assertIn("import encodings,json,sys", probe_args[2])
        self.assertTrue(result["artifactDigest"].startswith("sha256:"))
        att = result["attestation"]
        self.assertEqual(att["sourceDigest"], self.request.bundle_digest)
        self.assertEqual(att["rootfsDigest"], self.identity.digest)
        self.assertEqual(att["runscVersion"], "release-test")
        self.assertEqual(att["profile"], "python")
        self.assertEqual(att["network"], "deny")
        self.assertTrue(att["rootless"])
        self.assertEqual(att["hostRuntimeUser"], "cptr")
        self.assertEqual(att["cgroupDriver"], "systemd-transient")
        self.assertEqual(att["systemdSlice"], "cptr-sandbox.slice")
        self.assertEqual(att["runscCgroups"], "ignored-external-enforcement")
        argv = run.call_args.args[0]
        self.assertIn("--rootless=true", argv)
        self.assertIn("--ignore-cgroups", argv)
        self.assertIn("--network=none", argv)
        self.assertEqual(argv[-3], "run")

    def test_execution_bundle_restores_group_traverse_under_restrictive_umask(self):
        old_umask = os.umask(0o077)
        try:
            execution_root, bundle, probe_root, exec_root = self.dispatcher._prepare_execution_root()
        finally:
            os.umask(old_umask)
        try:
            self.assertEqual(os.stat(bundle).st_mode & 0o777, 0o750)
            self.assertEqual(os.stat(probe_root).st_mode & 0o777, 0o770)
            self.assertEqual(os.stat(exec_root).st_mode & 0o777, 0o770)
        finally:
            import shutil
            shutil.rmtree(execution_root, ignore_errors=True)

    def test_run_projects_json_input_and_requires_json_output(self):
        run_request = SandboxRequest(**{
            **self.request.__dict__,
            "operation": "run",
            "inputs": {"question": "life", "n": 42},
        })
        config = self.dispatcher.build_oci_config(run_request, self.staged)
        self.assertIn(
            'CPTR_TOOL_INPUT_JSON={"n":42,"question":"life"}',
            config["process"]["env"],
        )

        class Completed:
            returncode = 0
            stdout = b'{"answer":42}\n'
            stderr = b""

        with patch.object(self.dispatcher, "_cleanup"), patch(
            "cptr.services.capability_os.gvisor_dispatcher._run_bounded",
            side_effect=[BoundedProcessResult(0, b"Python 3.14.4\n", b""), Completed()],
        ):
            result = self.dispatcher(run_request, self.staged)
        self.assertEqual(result["output"], {"answer": 42})
        self.assertEqual(result["attestation"]["operation"], "run")

        class InvalidJson:
            returncode = 0
            stdout = b"not-json\n"
            stderr = b""

        with patch.object(self.dispatcher, "_cleanup"), patch(
            "cptr.services.capability_os.gvisor_dispatcher._run_bounded",
            side_effect=[BoundedProcessResult(0, b"Python 3.14.4\n", b""), InvalidJson()],
        ):
            with self.assertRaisesRegex(SandboxRuntimeUnavailable, "valid JSON"):
                self.dispatcher(run_request, self.staged)

    def test_build_rejects_runtime_inputs(self):
        request = SandboxRequest(**{**self.request.__dict__, "inputs": {"unexpected": True}})
        with self.assertRaisesRegex(SandboxRuntimeUnavailable, "build does not accept"):
            self.dispatcher.build_oci_config(request, self.staged)

    def test_nonzero_probe_reports_bounded_runtime_failure_detail(self):
        with patch.object(self.dispatcher, "_cleanup"), patch(
            "cptr.services.capability_os.gvisor_dispatcher._run_bounded",
            return_value=BoundedProcessResult(7, b"", b"cgroup setup denied"),
        ):
            with self.assertRaisesRegex(
                SandboxRuntimeUnavailable, "qualification probe.*status 7.*cgroup setup denied"
            ):
                self.dispatcher(self.request, self.staged)

    def test_nonzero_timeout_and_output_overflow_fail_closed(self):
        class Failed:
            returncode = 7
            stdout = b""
            stderr = b"bad"
        with patch.object(self.dispatcher, "_cleanup"), patch(
            "cptr.services.capability_os.gvisor_dispatcher._run_bounded", return_value=Failed()
        ):
            with self.assertRaises(SandboxRuntimeUnavailable):
                self.dispatcher(self.request, self.staged)

        class Huge:
            returncode = 0
            stdout = b"x" * 70000
            stderr = b""
        with patch.object(self.dispatcher, "_cleanup"), patch(
            "cptr.services.capability_os.gvisor_dispatcher._run_bounded", return_value=Huge()
        ):
            with self.assertRaises(SandboxRuntimeUnavailable):
                self.dispatcher(self.request, self.staged)


if __name__ == "__main__":
    unittest.main()
