import stat
import tempfile
import unittest
from pathlib import Path

from cptr.flowdeck.fdx import (
    FDXConfig,
    FDXPolicyError,
    FDXResult,
    run_fdx,
    run_optional_fdx,
    validate_workspace_jail,
)


class FDXTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.executable = self.root / "fdx"
        self.executable.write_text(
            "#!/bin/sh\n"
            "read payload\n"
            "printf '%s' '{\"protocol\":\"flowdeck-fdx/1\",\"payload\":\"ok\"}'\n"
        )
        self.executable.chmod(
            self.executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
        )

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **overrides):
        values = {
            "enabled": True,
            "executable": str(self.executable),
            "protocol": "flowdeck-fdx/1",
            "timeout_seconds": 2,
            "max_output_bytes": 1024,
        }
        values.update(overrides)
        return FDXConfig(**values)

    async def test_fdx_requires_protocol_and_absolute_executable(self):
        with self.assertRaises(FDXPolicyError):
            await run_fdx(
                {},
                workspace=str(self.workspace),
                config=self.config(protocol="wrong"),
            )
        with self.assertRaises(FDXPolicyError):
            await run_fdx(
                {},
                workspace=str(self.workspace),
                config=self.config(executable="fdx"),
            )

    async def test_workspace_jail_rejects_escape(self):
        with self.assertRaises(FDXPolicyError):
            validate_workspace_jail(str(self.root), str(self.workspace))
        with self.assertRaises(FDXPolicyError):
            await run_fdx(
                {},
                workspace=str(self.workspace),
                configured_root=str(self.workspace / "nested"),
                config=self.config(),
            )

    async def test_fdx_returns_bounded_structured_output(self):
        result = await run_fdx(
            {"task": "inspect"},
            workspace=str(self.workspace),
            configured_root=str(self.root),
            config=self.config(),
        )
        self.assertTrue(result.used_fdx)
        self.assertFalse(result.authoritative)
        self.assertEqual(result.output["protocol"], "flowdeck-fdx/1")

    async def test_enabled_fdx_failure_falls_back_to_native_result(self):
        async def fallback():
            return type(
                "Fallback",
                (),
                {
                    "status": "succeeded",
                    "output": {"native": True},
                    "authoritative": True,
                },
            )()

        result = await run_optional_fdx(
            {},
            workspace=str(self.workspace),
            config=self.config(executable=str(self.root / "missing")),
            fallback=fallback,
            configured_root=str(self.root),
        )
        self.assertFalse(result.used_fdx)
        self.assertTrue(result.authoritative)
        self.assertEqual(result.output, {"native": True})
        self.assertIsNotNone(result.fallback_reason)

    async def test_disabled_fdx_preserves_fallback_without_process(self):
        called = False

        async def fallback():
            nonlocal called
            called = True
            return FDXResult(
                status="succeeded",
                output={},
                authoritative=True,
                used_fdx=False,
            )

        result = await run_optional_fdx(
            {},
            workspace=str(self.workspace),
            config=FDXConfig(),
            fallback=fallback,
        )
        self.assertTrue(called)
        self.assertFalse(result.used_fdx)
        self.assertTrue(result.authoritative)
