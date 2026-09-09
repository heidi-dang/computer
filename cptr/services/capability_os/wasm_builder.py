"""WASM tool build pipeline for Capability OS Tool Forge.

Compiles generated tools to WebAssembly:
- TypeScript/JavaScript: uses javy CLI
- Python: uses py2wasm CLI

Falls back gracefully with WasmUnavailable if toolchain not installed.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class WasmUnavailable(Exception):
    """Raised when WASM toolchain (javy/py2wasm) is not installed."""


@dataclass
class WasmBuildResult:
    wasm_bytes: bytes | None
    success: bool
    error: str | None
    wasm_size_bytes: int
    build_tool: str
    is_stub: bool
    content_digest: str | None = None

    def __post_init__(self) -> None:
        if self.wasm_bytes and not self.content_digest:
            self.content_digest = hashlib.sha256(self.wasm_bytes).hexdigest()


SUPPORTED_LANGS = frozenset({"typescript", "javascript", "python"})


class WasmToolBuilder:
    """Compiles generated tool source to .wasm using available toolchain."""

    def is_available(self) -> bool:
        return self._javy_path() is not None or self._py2wasm_path() is not None

    def can_build(self, lang: str) -> bool:
        lang = lang.lower()
        if lang in ("typescript", "javascript"):
            return self._javy_path() is not None
        if lang == "python":
            return self._py2wasm_path() is not None
        return False

    def build(self, source_code: str, source_lang: str, tool_name: str) -> WasmBuildResult:
        lang = source_lang.lower()
        if lang in ("typescript", "javascript"):
            return self._build_js(source_code, tool_name)
        if lang == "python":
            return self._build_python(source_code, tool_name)
        raise WasmUnavailable("No WASM builder for language: " + source_lang)

    def _javy_path(self) -> str | None:
        return shutil.which("javy")

    def _py2wasm_path(self) -> str | None:
        return shutil.which("py2wasm")

    def _build_js(self, source_code: str, tool_name: str) -> WasmBuildResult:
        javy = self._javy_path()
        if javy is None:
            raise WasmUnavailable("javy not found in PATH")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / (tool_name + ".js")
            out = Path(tmp) / (tool_name + ".wasm")
            src.write_text(source_code)
            r = subprocess.run([javy, "compile", str(src), "-o", str(out)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                return WasmBuildResult(wasm_bytes=None, success=False,
                                       error="javy: " + r.stderr,
                                       wasm_size_bytes=0, build_tool="javy", is_stub=False)
            wasm_bytes = out.read_bytes()
            return WasmBuildResult(wasm_bytes=wasm_bytes, success=True, error=None,
                                   wasm_size_bytes=len(wasm_bytes), build_tool="javy", is_stub=False)

    def _build_python(self, source_code: str, tool_name: str) -> WasmBuildResult:
        py2wasm = self._py2wasm_path()
        if py2wasm is None:
            raise WasmUnavailable("py2wasm not found in PATH")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / (tool_name + ".py")
            out = Path(tmp) / (tool_name + ".wasm")
            src.write_text(source_code)
            r = subprocess.run([py2wasm, str(src), "-o", str(out)],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return WasmBuildResult(wasm_bytes=None, success=False,
                                       error="py2wasm: " + r.stderr,
                                       wasm_size_bytes=0, build_tool="py2wasm", is_stub=False)
            wasm_bytes = out.read_bytes()
            return WasmBuildResult(wasm_bytes=wasm_bytes, success=True, error=None,
                                   wasm_size_bytes=len(wasm_bytes), build_tool="py2wasm", is_stub=False)
