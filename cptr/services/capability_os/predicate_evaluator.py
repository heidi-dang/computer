"""Bounded predicate evaluator for Capability VM.

NEVER uses eval() on arbitrary input.
Uses ast module: parse -> safety check -> compile -> eval on pre-validated AST.

Supported: comparisons, boolean ops, field access, subscript, membership,
           arithmetic, constants, whitelisted string methods, ternary.
Forbidden: arbitrary function calls, imports, loops, assignments, comprehensions,
           lambdas, class/def, global/nonlocal.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any


class PredicateError(Exception):
    """Raised when predicate is malformed or uses disallowed syntax."""


@dataclass
class PredicateContext:
    """Evaluation context: variables available to predicates."""
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    effects_applied: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


_ALLOWED_NODES = frozenset({
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv, ast.Pow,
    ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Constant,
    ast.Name,
    ast.Attribute,
    ast.Subscript,
    ast.Index,
    ast.Slice,
    ast.List, ast.Tuple, ast.Set,
    ast.IfExp,
    ast.Load, ast.Store, ast.Del,
})

_ALLOWED_STRING_METHODS = frozenset({
    "startswith", "endswith", "lower", "upper", "strip",
    "lstrip", "rstrip", "replace", "split", "count", "find", "index",
})


class PredicateEvaluator:
    """Safe bounded predicate evaluator using Python ast."""

    def evaluate(self, expression: str, context: PredicateContext) -> bool:
        if not expression or not expression.strip():
            return True
        try:
            tree = ast.parse(expression.strip(), mode="eval")
        except SyntaxError as exc:
            raise PredicateError("Syntax error in predicate: " + str(exc)) from exc
        self._check_ast_safety(tree)
        ns = self._build_namespace(context)
        try:
            result = eval(compile(tree, "<predicate>", "eval"), {"__builtins__": {}}, ns)  # noqa: S307
        except PredicateError:
            raise
        except Exception as exc:
            raise PredicateError("Evaluation error: " + str(exc)) from exc
        return bool(result)

    def _check_ast_safety(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            node_type = type(node)
            if node_type in _ALLOWED_NODES:
                continue
            if isinstance(node, ast.Call):
                if (isinstance(node.func, ast.Attribute)
                        and node.func.attr in _ALLOWED_STRING_METHODS
                        and not node.keywords):
                    continue
                raise PredicateError(
                    "Function calls are not allowed in predicates. "
                    "Only string methods are permitted: " + str(sorted(_ALLOWED_STRING_METHODS))
                )
            raise PredicateError(
                "Disallowed expression type: " + node_type.__name__ + ". "
                "Predicates support comparisons, boolean ops, field access, and constants only."
            )

    def _build_namespace(self, context: PredicateContext) -> dict:
        return {
            "inputs": _AttrDict(context.inputs),
            "outputs": _AttrDict(context.outputs),
            "effects_applied": context.effects_applied,
            "metadata": _AttrDict(context.metadata),
            "True": True,
            "False": False,
            "None": None,
        }


class _AttrDict(dict):
    """Dict with attribute-style access: d.key == d['key']."""
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError("No field '" + name + "' in context") from None
