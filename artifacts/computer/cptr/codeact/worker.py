"""Standalone restricted worker process.

The worker intentionally imports only the standard library and communicates
with CPTR through JSON lines. Capability implementations never enter it.
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import sys
import traceback
from contextlib import redirect_stdout
from io import StringIO


ALLOWED_IMPORTS = {
    "collections",
    "datetime",
    "functools",
    "itertools",
    "json",
    "math",
    "re",
    "statistics",
}
BLOCKED_NAMES = {
    "__builtins__", "__import__", "compile", "eval", "exec", "globals", "locals",
    "open", "input", "getattr", "setattr", "delattr", "vars", "dir", "breakpoint",
    "help", "exit", "quit", "memoryview", "object", "type", "super",
}


class SandboxError(Exception):
    pass


def _validate(code: str, max_chars: int) -> None:
    if not isinstance(code, str) or not code.strip():
        raise SandboxError("generated code must be non-empty")
    if len(code) > max_chars:
        raise SandboxError("generated code exceeds the configured size limit")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxError(f"syntax error: {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and (node.id in BLOCKED_NAMES or node.id.startswith("__")):
            raise SandboxError(f"name is not allowed: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise SandboxError(f"attribute is not allowed: {node.attr}")
        if isinstance(node, ast.Import):
            if any(alias.name not in ALLOWED_IMPORTS for alias in node.names):
                raise SandboxError("import is not allowed")
        if isinstance(node, ast.ImportFrom) and (node.level or node.module not in ALLOWED_IMPORTS):
            raise SandboxError("relative or unapproved import is not allowed")


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name not in ALLOWED_IMPORTS:
        raise ImportError(f"import is not allowed: {name}")
    return builtins.__import__(name, globals, locals, fromlist, level)


def _send(payload: dict, stream) -> None:
    stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
    stream.flush()


def _main() -> None:
    protocol_stdout = sys.stdout
    max_chars = int(os.environ.get("CPTR_CODEACT_MAX_CODE_CHARS", "24000"))
    max_output = int(os.environ.get("CPTR_CODEACT_MAX_OUTPUT_CHARS", "16000"))
    max_calls = int(os.environ.get("CPTR_CODEACT_MAX_CAPABILITY_CALLS", "64"))
    namespace: dict[str, object] = {}
    call_count = 0

    def capability(name: str):
        def invoke(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count > max_calls:
                raise SandboxError("capability call limit exceeded")
            _send({"type": "capability_call", "name": name, "arguments": kwargs}, protocol_stdout)
            response = sys.stdin.readline()
            if not response:
                raise SandboxError("capability host disconnected")
            message = json.loads(response)
            if not message.get("ok"):
                raise SandboxError(str(message.get("error", "capability denied")))
            return message.get("result")

        return invoke

    class Files:
        read = staticmethod(capability("files.read"))
        list = staticmethod(capability("files.list"))
        search = staticmethod(capability("files.search"))

    class Git:
        status = staticmethod(capability("git.status"))
        diff = staticmethod(capability("git.diff"))

    class Cptr:
        files = Files()
        git = Git()

    safe_builtins = {
        name: getattr(builtins, name)
        for name in (
            "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
            "hash", "int", "isinstance", "len", "list", "map", "max", "min",
            "print", "range", "repr", "reversed", "round", "set", "sorted",
            "str", "sum", "tuple", "zip",
        )
    }
    safe_builtins["__import__"] = _safe_import
    namespace.update({"__builtins__": safe_builtins, "cptr": Cptr()})

    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("type") == "shutdown":
                _send({"type": "shutdown"}, protocol_stdout)
                return
            if request.get("type") != "execute":
                raise SandboxError("unknown worker request")
            _validate(request.get("code", ""), max_chars)
            output = StringIO()
            before_calls = call_count
            with redirect_stdout(output):
                exec(compile(request["code"], "<codeact>", "exec"), namespace, namespace)
            text = output.getvalue()
            encoded = text.encode("utf-8", errors="replace")
            truncated = len(encoded) > max_output
            if truncated:
                text = encoded[:max_output].decode("utf-8", errors="ignore") + "\n… [output truncated]"
            _send({
                "type": "result",
                "output": text,
                "capability_calls": call_count - before_calls,
                "truncated": truncated,
            }, protocol_stdout)
        except BaseException as exc:
            safe_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            _send({
                "type": "error",
                "error": safe_error,
                "traceback": "".join(traceback.format_exception_only(type(exc), exc))[:800],
            }, protocol_stdout)


if __name__ == "__main__":
    _main()
