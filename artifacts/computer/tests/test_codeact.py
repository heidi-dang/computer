"""Security, persistence, isolation, and lifecycle qualification for CodeAct."""

from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path

from cptr.codeact.capabilities import sdk_from_tool_context
from cptr.codeact.contracts import CodeActConfig, CodeActIdentity, CodeActLimits, CodeActMode
from cptr.codeact.repl import CodeActCapabilityError, CodeActRepl, ReadOnlyCapabilitySDK
from cptr.codeact.runner import run_read_only_attempt
from cptr.codeact.benchmark import BenchmarkCase, run_same_model_ab
from cptr.codeact.telemetry import ExecutionTelemetry
from cptr.codeact.sandbox import CodeActSandboxError, validate_program


def enabled_config(**kwargs):
    values = {"wall_seconds": 3, "cpu_seconds": 2}
    values.update(kwargs)
    limits = CodeActLimits(**values)
    return CodeActConfig(mode=CodeActMode.READ_ONLY, allowed_roles=frozenset({"security-auditor"}), limits=limits)


class CodeActSandboxTests(unittest.TestCase):
    def test_default_is_disabled_and_role_controlled(self):
        config = CodeActConfig()
        self.assertFalse(config.enabled)
        self.assertFalse(config.allows_role("security-auditor"))
        self.assertTrue(enabled_config().allows_role("security-auditor"))

    def test_validator_denies_escape_primitives(self):
        for code in (
            "import os",
            "import subprocess",
            "import socket",
            "import ctypes",
            "import multiprocessing",
            "import pickle",
            "import importlib",
            "open('/etc/passwd')",
            "__import__('os')",
            "getattr(cptr, 'files')",
            "cptr.files.__dict__",
            "exec('pass')",
            "globals()",
            "print(__import__('os').environ)",
        ):
            with self.subTest(code=code):
                with self.assertRaises(CodeActSandboxError):
                    validate_program(code)

    def test_only_audited_imports_are_allowed(self):
        validate_program("import math\nprint(math.sqrt(9))")
        with self.assertRaises(CodeActSandboxError):
            validate_program("from pathlib import Path")


class CodeActReplTests(unittest.IsolatedAsyncioTestCase):
    def identity(self, attempt: str = "attempt") -> CodeActIdentity:
        return CodeActIdentity(
            user_id="user-a",
            workspace="/workspace-a",
            task_id="task-a",
            attempt_id=attempt,
            model_id="fixture-model",
        )

    async def test_state_survives_blocks_and_capability_results_remain_objects(self):
        async def read(**kwargs):
            return {"path": kwargs["path"], "lines": [1, 2, 3]}

        sdk = ReadOnlyCapabilitySDK.from_handlers({"files.read": read})
        repl = CodeActRepl(identity=self.identity(), sdk=sdk, config=enabled_config())
        try:
            await repl.execute("files = cptr.files.read(path='auth.py')")
            result = await repl.execute("print(files['path'], sum(files['lines']))")
            self.assertIn("auth.py 6", result.output)
            self.assertEqual(len(repl.capability_calls), 1)
            self.assertEqual(repl.capability_calls[0].identity.attempt_id, "attempt")
        finally:
            await repl.close(force=True)
        self.assertTrue(repl.closed)

    async def test_unknown_capability_is_denied_without_service_leak(self):
        repl = CodeActRepl(
            identity=self.identity(),
            sdk=ReadOnlyCapabilitySDK.from_handlers({}),
            config=enabled_config(),
        )
        try:
            with self.assertRaises(CodeActSandboxError):
                await repl.execute("cptr.git.status()")
        finally:
            await repl.close(force=True)

    async def test_cancellation_and_timeout_destroy_worker(self):
        repl = CodeActRepl(
            identity=self.identity(),
            sdk=ReadOnlyCapabilitySDK.from_handlers({}),
            config=enabled_config(wall_seconds=0.25),
        )
        with self.assertRaises(TimeoutError):
            await repl.execute("while True:\n    pass")
        self.assertTrue(repl.closed)
        self.assertIsNone(repl._process)

    async def test_sessions_do_not_share_state(self):
        sdk = ReadOnlyCapabilitySDK.from_handlers({})
        first = CodeActRepl(identity=self.identity("one"), sdk=sdk, config=enabled_config())
        second = CodeActRepl(identity=self.identity("two"), sdk=sdk, config=enabled_config())
        try:
            await first.execute("secret = 'one'")
            with self.assertRaises(CodeActSandboxError):
                await second.execute("print(secret)")
        finally:
            await first.close(force=True)
            await second.close(force=True)

    async def test_attempt_emits_identity_bound_events_and_tears_down(self):
        events = []

        async def emit(event):
            events.append(event)

        identity = self.identity("attempt-1")
        result = await run_read_only_attempt(
            identity=identity,
            sdk=ReadOnlyCapabilitySDK.from_handlers({}),
            program="print('qualified')",
            config=enabled_config(),
            emit=emit,
        )
        self.assertEqual(result.output.strip(), "qualified")
        self.assertEqual([event["type"] for event in events], ["codeact_started", "codeact_completed"])
        self.assertTrue(all(event["attempt_id"] == "attempt-1" for event in events))


class CodeActAdapterTests(unittest.TestCase):
    def test_adapter_exposes_only_existing_read_only_native_names(self):
        sdk = sdk_from_tool_context({"workspace": str(Path.cwd())})
        self.assertEqual(sdk.names, frozenset({"files.read", "files.list", "files.search"}))
        self.assertNotIn("files.write", sdk.names)
        self.assertNotIn("shell.run", sdk.names)

    def test_benchmark_pairs_modes_and_records_correctness(self):
        async def run():
            async def native(case, telemetry: ExecutionTelemetry):
                telemetry.model_invocations = 1
                return case.expected

            async def codeact(case, telemetry: ExecutionTelemetry):
                telemetry.capability_calls = 1
                return case.expected

            return await run_same_model_ab(
                [BenchmarkCase("file-list", "list files", ["a.py"])],
                native_runner=native,
                codeact_runner=codeact,
            )

        observations = asyncio.run(run())
        self.assertEqual([item.mode for item in observations], [CodeActMode.DISABLED, CodeActMode.READ_ONLY])
        self.assertTrue(all(item.telemetry["correctness"] for item in observations))


if __name__ == "__main__":
    unittest.main()