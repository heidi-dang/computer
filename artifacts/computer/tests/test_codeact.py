"""Security, persistence, isolation, and lifecycle qualification for CodeAct."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cptr.codeact.capabilities import sdk_from_tool_context
from cptr.codeact.contracts import CodeActConfig, CodeActIdentity, CodeActLimits, CodeActMode
from cptr.codeact.contracts import QUALIFICATION_OBSERVATIONS, QUALIFICATION_SECURITY_CASES
from cptr.codeact.repl import CodeActRepl, ReadOnlyCapabilitySDK
from cptr.codeact.runner import run_read_only_attempt
from cptr.codeact.benchmark import (
    BenchmarkCase,
    ProviderMeasurement,
    run_provider_benchmark,
    run_same_model_ab,
)
from cptr.codeact.telemetry import ExecutionTelemetry
from cptr.codeact.sandbox import CodeActSandboxError, validate_program
from cptr.codeact.qualification import (
    FIXTURE_FILES,
    FixtureReadOnlyTools,
    OpenAICompatibleQualificationRunner,
    QUALIFICATION_CASES,
    _final_value,
    _program_from_response,
)


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

    def test_model_requires_complete_passing_qualification_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "qualification.json"
            config = CodeActConfig(
                mode=CodeActMode.READ_ONLY,
                allowed_roles=frozenset({"security-auditor"}),
                qualification_report_path=str(report_path),
            )
            self.assertFalse(config.allows_qualified_model("model-a"))
            report_path.write_text(json.dumps({
                "provider_backed": True,
                "model_id": "model-a",
                "decision": "keep-disabled",
                "score": 80.0,
                "observations": [],
                "security": [],
            }))
            self.assertFalse(config.allows_qualified_model("model-a"))
            report_path.write_text(json.dumps({
                "provider_backed": True,
                "model_id": "model-a",
                "decision": "enable-read-only",
                "score": 100.0,
                "observations": [
                    {"case": case, "mode": mode, "telemetry": {"correctness": True}}
                    for case, mode in QUALIFICATION_OBSERVATIONS
                ],
                "security": [
                    {"name": name, "category": category, "blocked": True}
                    for name, category in QUALIFICATION_SECURITY_CASES
                ],
            }))
            self.assertTrue(config.allows_qualified_model("model-a"))
            self.assertFalse(config.allows_qualified_model("another-model"))

    def test_model_rejects_partial_or_duplicate_qualification_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "qualification.json"
            config = CodeActConfig(qualification_report_path=str(report_path))
            report_path.write_text(json.dumps({
                "provider_backed": True,
                "model_id": "model-a",
                "decision": "enable-read-only",
                "score": 100.0,
                "observations": [
                    {"case": "release-label", "mode": "disabled", "telemetry": {"correctness": True}}
                ],
                "security": [{"name": "import-os", "category": "import", "blocked": True}] * 7,
            }))
            self.assertFalse(config.allows_qualified_model("model-a"))

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

    async def test_disabled_context_does_not_start_worker(self):
        started = False

        def process_factory(*args, **kwargs):
            nonlocal started
            started = True
            raise AssertionError("disabled CodeAct must not spawn a worker")

        repl = CodeActRepl(
            identity=self.identity(),
            sdk=ReadOnlyCapabilitySDK.from_handlers({}),
            config=CodeActConfig(),
            process_factory=process_factory,
        )
        with self.assertRaises(CodeActSandboxError):
            async with repl:
                pass
        self.assertFalse(started)
        self.assertTrue(repl.closed)

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
                model_id="same-model",
            )

        observations = asyncio.run(run())
        self.assertEqual([item.mode for item in observations], [CodeActMode.DISABLED, CodeActMode.READ_ONLY])
        self.assertTrue(all(item.telemetry["correctness"] for item in observations))
        self.assertEqual({item.telemetry["model_id"] for item in observations}, {"same-model"})

    def test_provider_benchmark_reports_metrics_security_and_decision(self):
        async def run():
            async def provider(case, mode, telemetry):
                telemetry.cycles = 2
                return ProviderMeasurement(
                    result=case.expected,
                    input_tokens=10,
                    output_tokens=5,
                    cycles=2,
                    capability_calls=1 if mode is CodeActMode.READ_ONLY else 0,
                    context_bytes=42,
                )

            return await run_provider_benchmark(
                [BenchmarkCase("sum", "calculate", 3)],
                model_id="same-model",
                provider_runner=provider,
                provider_backed=True,
            )

        report = asyncio.run(run())
        self.assertEqual(report.decision, "enable-read-only")
        self.assertEqual(report.score, 100.0)
        self.assertEqual(len(report.security), 7)
        self.assertTrue(all(item.blocked for item in report.security))
        self.assertEqual(report.observations[0].telemetry["cycles"], 2)
        self.assertEqual(report.observations[0].telemetry["context_bytes"], 42)

    def test_provider_benchmark_records_provider_failure_as_incorrect_observation(self):
        async def run():
            async def failing_provider(case, mode, telemetry):
                raise RuntimeError("provider unavailable")

            return await run_provider_benchmark(
                [BenchmarkCase("sum", "calculate", 3)],
                model_id="same-model",
                provider_runner=failing_provider,
                provider_backed=True,
            )

        report = asyncio.run(run())
        self.assertEqual(report.decision, "keep-disabled")
        self.assertEqual(len(report.observations), 2)
        self.assertTrue(all(str(item.result).startswith("ERROR: RuntimeError") for item in report.observations))

    def test_live_qualification_corpus_is_fixed_and_read_only(self):
        self.assertEqual([case.name for case in QUALIFICATION_CASES], [
            "release-label",
            "inventory-total",
            "ready-owner",
        ])
        self.assertEqual(set(FIXTURE_FILES), {"release.txt", "inventory.txt", "ownership.txt"})
        self.assertEqual(_final_value("thinking\nFINAL: ORCHID\n"), "ORCHID")
        self.assertEqual(_program_from_response("```python\nprint('FINAL: ORCHID')\n```"), "print('FINAL: ORCHID')")
        self.assertEqual(
            _program_from_response("<think>reasoning</think>\nprint('FINAL: ORCHID')"),
            "print('FINAL: ORCHID')",
        )

    def test_fixture_tools_do_not_expose_unlisted_capabilities(self):
        async def run():
            fixtures = FixtureReadOnlyTools(dict(FIXTURE_FILES))
            self.assertIn("ORCHID", await fixtures.invoke("read_file", {"path": "release.txt"}))
            with self.assertRaises(PermissionError):
                await fixtures.invoke("shell.run", {})

        asyncio.run(run())

    def test_live_codeact_qualification_repairs_incorrect_program(self):
        async def run():
            runner = OpenAICompatibleQualificationRunner(
                SimpleNamespace(
                    runtime_model="fixture-model",
                    full_model_id="fixture-model",
                    connection={},
                ),
                FixtureReadOnlyTools(dict(FIXTURE_FILES)),
            )
            responses = iter(
                [
                    "data = cptr.files.read(path='inventory.txt')\nprint('FINAL: 0')",
                    "data = cptr.files.read(path='inventory.txt')\nprint('FINAL: 18')",
                ]
            )

            async def complete(_payload):
                return (
                    {
                        "choices": [{"message": {"content": next(responses)}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    },
                    100,
                )

            runner._complete = complete
            measurement = await runner(
                QUALIFICATION_CASES[1], CodeActMode.READ_ONLY, None
            )
            self.assertEqual(measurement.result, "18")
            self.assertEqual(measurement.cycles, 2)
            self.assertEqual(measurement.capability_calls, 2)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()