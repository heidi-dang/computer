import ast
import asyncio
import json
import unittest
from pathlib import Path
from typing import Optional


ADAPTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "capability-os"
    / "os-mcp"
    / "server.py"
)


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"missing function {name}")


def _default_for(function: ast.FunctionDef | ast.AsyncFunctionDef, parameter: str):
    names = [argument.arg for argument in function.args.args]
    first_default = len(names) - len(function.args.defaults)
    index = names.index(parameter)
    if index < first_default:
        raise LookupError(f"parameter {parameter} is required")
    return function.args.defaults[index - first_default]


def _standalone_function(tree: ast.Module, name: str, namespace: dict):
    function = _function(tree, name)
    function.decorator_list = []
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    scope = dict(namespace)
    exec(compile(module, str(ADAPTER_PATH), "exec"), scope)
    return scope[name]


class CapabilityOsMcpAdapterDeployTests(unittest.TestCase):
    def test_adapter_keeps_six_core_tools_plus_parallel_spawn_and_explicit_bootstrap(self):
        self.assertTrue(ADAPTER_PATH.is_file(), "versioned os-mcp adapter is missing")
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)

        tool_names = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("cptr_")
        }
        self.assertEqual(
            tool_names,
            {
                "cptr_inspect",
                "cptr_resolve",
                "cptr_forge",
                "cptr_execute",
                "cptr_acquire",
                "cptr_reflect",
            },
        )

        inspect = _function(tree, "cptr_inspect")
        with self.assertRaises(LookupError):
            _default_for(inspect, "task_id")

        forge = _function(tree, "cptr_forge")
        forge_names = [argument.arg for argument in forge.args.args]
        self.assertEqual(forge_names[0], "operation")
        task_default = _default_for(forge, "task_id")
        self.assertIsInstance(task_default, ast.Constant)
        self.assertIsNone(task_default.value)

        self.assertIn('operation == "bootstrap"', source)
        self.assertIn('"operation": "bootstrap"', source)
        self.assertNotIn("bootstrap must not include task_id", source)
        self.assertIn('version="2.3.0"', source)
        spawn = _function(tree, "spawn_multiple_subagents")
        self.assertIsInstance(spawn, ast.AsyncFunctionDef)
        self.assertIn("/spawn-multiple-subagents", source)
        self.assertIn("asyncio.Event", source)
        self.assertIn("asyncio.create_task", source)
        self.assertIn("asyncio.gather", source)
        self.assertIn("ctx.sample", source)
        self.assertIn("tools=_bound_sampling_tools(child_task_id)", source)
        self.assertIn("tool_concurrency=0", source)
        self.assertNotIn("cptr_agent_task", source)
        self.assertNotIn("AgentService", source)
        self.assertNotIn("OpenAISamplingHandler", source)

    def test_bootstrap_tolerates_stale_required_task_id_but_never_forwards_it(self):
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = []

        def parse_json(raw: str, label: str):
            value = json.loads(raw)
            if not isinstance(value, dict):
                return None, {"error": f"{label} JSON must decode to an object"}
            return value, None

        def request(method: str, path: str, *, timeout: float, **kwargs):
            calls.append((method, path, timeout, kwargs))
            return {"bootstrapped": True}

        forge = _standalone_function(
            tree,
            "cptr_forge",
            {"Optional": Optional, "_json_object": parse_json, "_request": request},
        )
        result = forge(
            operation="bootstrap",
            task_id="required-by-stale-client-schema",
        )

        self.assertEqual(result, {"bootstrapped": True})
        self.assertEqual(
            calls,
            [
                (
                    "POST",
                    "/forge",
                    15,
                    {"json": {"operation": "bootstrap", "payload": {}}},
                )
            ],
        )

    def test_spawn_multiple_subagents_enters_all_sampling_branches_concurrently(self):
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)

        def noop(*args, **kwargs):
            return None

        async def prepare_request(method: str, path: str, *, timeout: float, **kwargs):
            self.assertEqual((method, path), ("POST", "/spawn-multiple-subagents"))
            objectives = kwargs["json"]["objectives"]
            return {
                "task": {"taskId": "parent"},
                "dispatch": {
                    "mode": "mcp-client-sampling",
                    "parallel": True,
                    "subagents": [
                        {"task": {"taskId": f"child-{index + 1}"}}
                        for index in range(len(objectives))
                    ],
                },
            }

        spawn = _standalone_function(
            tree,
            "spawn_multiple_subagents",
            {
                "asyncio": asyncio,
                "Context": object,
                "_required_task_id": lambda value: (value.strip(), None),
                "_async_request": prepare_request,
                "_bound_sampling_tools": lambda task_id: [noop],
            },
        )

        class SampleResult:
            def __init__(self, text: str) -> None:
                self.text = text

        class FakeContext:
            def __init__(self) -> None:
                self.active = 0
                self.max_active = 0
                self.started = 0
                self.release = asyncio.Event()

            async def sample(self, *, messages: str, tools, **kwargs):
                self.active += 1
                self.started += 1
                self.max_active = max(self.max_active, self.active)
                if self.started == 3:
                    self.release.set()
                await asyncio.wait_for(self.release.wait(), timeout=0.5)
                self.active -= 1
                return SampleResult(messages.split("OBJECTIVE:\n", 1)[-1])

        async def exercise():
            ctx = FakeContext()
            result = await spawn(
                "parent",
                ["alpha", "beta", "gamma"],
                ctx,
                max_tokens=256,
            )
            return ctx, result

        ctx, result = asyncio.run(exercise())
        self.assertEqual(ctx.started, 3)
        self.assertEqual(ctx.max_active, 3)
        self.assertEqual(result["requested"], 3)
        self.assertEqual(result["completed"], 3)
        self.assertEqual(result["failed"], 0)
        self.assertTrue(result["dispatch"]["parallel"])
        self.assertTrue(result["dispatch"]["samplingRequestsIssuedConcurrently"])
        self.assertFalse(result["dispatch"]["hostParallelInferenceVerified"])
        self.assertEqual(result["dispatch"]["nativeSubagentMapping"], "client-defined")
        self.assertEqual(
            [item["output"] for item in result["results"]],
            ["alpha", "beta", "gamma"],
        )


if __name__ == "__main__":
    unittest.main()
