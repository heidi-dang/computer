"""Hostile-code validation used before any worker execution."""

from __future__ import annotations

import ast
from dataclasses import dataclass


class CodeActSandboxError(ValueError):
    """Generated code is not within the server-owned sandbox subset."""


ALLOWED_IMPORTS = frozenset({"collections", "datetime", "functools", "itertools", "json", "math", "re", "statistics"})
BLOCKED_NAMES = frozenset(
    {
        "__builtins__",
        "__import__",
        "compile",
        "eval",
        "exec",
        "globals",
        "locals",
        "open",
        "input",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "dir",
        "breakpoint",
        "help",
        "exit",
        "quit",
        "memoryview",
        "object",
        "type",
        "super",
    }
)
BLOCKED_ATTRIBUTES = frozenset(
    {
        "__class__",
        "__dict__",
        "__bases__",
        "__subclasses__",
        "__globals__",
        "__code__",
        "__getattribute__",
        "__mro__",
    }
)


@dataclass(frozen=True)
class ValidatedProgram:
    code: str
    node_count: int


class _Validator(ast.NodeVisitor):
    def __init__(self, max_nodes: int = 20_000):
        self.max_nodes = max_nodes
        self.node_count = 0

    def generic_visit(self, node: ast.AST):
        self.node_count += 1
        if self.node_count > self.max_nodes:
            raise CodeActSandboxError("code contains too many syntax nodes")
        super().generic_visit(node)

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            if alias.name not in ALLOWED_IMPORTS:
                raise CodeActSandboxError(f"import is not allowed: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.level or node.module not in ALLOWED_IMPORTS:
            raise CodeActSandboxError("relative or unapproved import is not allowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if node.id in BLOCKED_NAMES or node.id.startswith("__"):
            raise CodeActSandboxError(f"name is not allowed: {node.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr in BLOCKED_ATTRIBUTES or node.attr.startswith("__"):
            raise CodeActSandboxError(f"attribute is not allowed: {node.attr}")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, (bytes, bytearray)):
            raise CodeActSandboxError("binary constants are not allowed")
        self.generic_visit(node)


def validate_program(code: str, *, max_chars: int = 24_000) -> ValidatedProgram:
    if not isinstance(code, str) or not code.strip():
        raise CodeActSandboxError("generated code must be non-empty")
    if len(code) > max_chars:
        raise CodeActSandboxError("generated code exceeds the configured size limit")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise CodeActSandboxError(f"syntax error: {exc.msg}") from exc
    validator = _Validator()
    validator.visit(tree)
    return ValidatedProgram(code=code, node_count=validator.node_count)
