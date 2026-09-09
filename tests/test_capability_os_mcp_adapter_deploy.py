import ast
import unittest
from pathlib import Path


ADAPTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "capability-os"
    / "os-mcp"
    / "server.py"
)


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function {name}")


def _default_for(function: ast.FunctionDef, parameter: str):
    names = [argument.arg for argument in function.args.args]
    first_default = len(names) - len(function.args.defaults)
    index = names.index(parameter)
    if index < first_default:
        raise LookupError(f"parameter {parameter} is required")
    return function.args.defaults[index - first_default]


class CapabilityOsMcpAdapterDeployTests(unittest.TestCase):
    def test_adapter_keeps_six_tool_surface_and_exposes_explicit_bootstrap(self):
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
        self.assertIn('version="2.2.0"', source)


if __name__ == "__main__":
    unittest.main()
