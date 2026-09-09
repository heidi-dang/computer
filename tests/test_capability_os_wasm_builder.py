"""Tests for the WASM tool builder pipeline."""
from __future__ import annotations

import pytest

from cptr.services.capability_os.wasm_builder import (
    WasmBuildResult,
    WasmToolBuilder,
    WasmUnavailable,
)


def test_is_available_returns_bool():
    result = WasmToolBuilder().is_available()
    assert isinstance(result, bool)


def test_can_build_all_langs():
    builder = WasmToolBuilder()
    for lang in ("typescript", "javascript", "python", "rust", "go", ""):
        assert isinstance(builder.can_build(lang), bool)


def test_build_result_fields():
    result = WasmBuildResult(
        wasm_bytes=b"\x00asm\x01\x00\x00\x00",
        success=True,
        error=None,
        wasm_size_bytes=8,
        build_tool="javy",
        is_stub=False,
    )
    assert result.content_digest is not None
    assert len(result.content_digest) == 64


def test_build_result_no_bytes():
    result = WasmBuildResult(
        wasm_bytes=None,
        success=False,
        error="build failed",
        wasm_size_bytes=0,
        build_tool=None,
        is_stub=False,
    )
    assert result.content_digest is None


def test_unknown_lang_raises():
    builder = WasmToolBuilder()
    with pytest.raises(WasmUnavailable):
        builder.build("code", "rust", "test")


def test_typescript_unavailable():
    builder = WasmToolBuilder()
    if not builder.can_build("typescript"):
        with pytest.raises(WasmUnavailable):
            builder.build("console.log('hi')", "typescript", "test_tool")
    else:
        pytest.skip("javy is installed; unavailability path not testable")


def test_python_unavailable():
    builder = WasmToolBuilder()
    if not builder.can_build("python"):
        with pytest.raises(WasmUnavailable):
            builder.build("print('hi')", "python", "test_tool")
    else:
        pytest.skip("py2wasm is installed; unavailability path not testable")


def test_wasm_unavailable_is_exception():
    err = WasmUnavailable("x")
    assert isinstance(err, Exception)
