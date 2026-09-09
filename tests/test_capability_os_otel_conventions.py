"""Tests for cptr.* OTel semantic conventions (Capability OS)."""

from __future__ import annotations

import inspect

import pytest

from cptr.services.capability_os.otel_conventions import (
    CptrAttributes,
    CptrSpanNames,
    _NoopSpan,
    _NoopTracer,
    get_tracer,
)


def _str_constants(cls) -> list[str]:
    """Return all public string class-level constants from *cls*."""
    return [
        v
        for k, v in inspect.getmembers(cls)
        if not k.startswith("_") and isinstance(v, str)
    ]


# ---------------------------------------------------------------------------
# CptrSpanNames
# ---------------------------------------------------------------------------

class TestCptrSpanNames:
    def test_all_start_with_cptr(self):
        for val in _str_constants(CptrSpanNames):
            assert val.startswith("cptr."), f"{val!r} must start with 'cptr.'"

    def test_all_are_strings(self):
        for val in _str_constants(CptrSpanNames):
            assert isinstance(val, str)

    def test_at_least_10_span_names(self):
        names = _str_constants(CptrSpanNames)
        assert len(names) >= 10, f"Expected >= 10 span names, got {len(names)}"

    def test_all_have_at_least_two_dots(self):
        for val in _str_constants(CptrSpanNames):
            dot_count = val.count(".")
            assert dot_count >= 2, f"{val!r} has only {dot_count} dot(s); need >= 2"


# ---------------------------------------------------------------------------
# CptrAttributes
# ---------------------------------------------------------------------------

class TestCptrAttributes:
    def test_all_start_with_cptr(self):
        for val in _str_constants(CptrAttributes):
            assert val.startswith("cptr."), f"{val!r} must start with 'cptr.'"

    def test_all_are_strings(self):
        for val in _str_constants(CptrAttributes):
            assert isinstance(val, str)

    def test_at_least_15_attribute_keys(self):
        keys = _str_constants(CptrAttributes)
        assert len(keys) >= 15, f"Expected >= 15 attribute keys, got {len(keys)}"


# ---------------------------------------------------------------------------
# get_tracer / noop infrastructure
# ---------------------------------------------------------------------------

class TestGetTracer:
    def test_returns_non_none(self):
        tracer = get_tracer()
        assert tracer is not None

    def test_default_name(self):
        # Should not raise regardless of OTel availability
        tracer = get_tracer()
        assert tracer is not None

    def test_custom_name(self):
        tracer = get_tracer("cptr.test.scope")
        assert tracer is not None


class TestNoopSpan:
    def test_context_manager_returns_self(self):
        span = _NoopSpan()
        with span as s:
            assert s is span

    def test_set_attribute_does_not_raise(self):
        span = _NoopSpan()
        span.set_attribute("cptr.task.id", "abc123")

    def test_set_status_does_not_raise(self):
        span = _NoopSpan()
        span.set_status("OK")

    def test_record_exception_does_not_raise(self):
        span = _NoopSpan()
        span.record_exception(ValueError("boom"))


class TestNoopTracer:
    def test_start_as_current_span_returns_noop_span(self):
        tracer = _NoopTracer()
        span = tracer.start_as_current_span("cptr.test.op")
        assert isinstance(span, _NoopSpan)

    def test_noop_span_usable_as_context_manager(self):
        tracer = _NoopTracer()
        with tracer.start_as_current_span("cptr.test.op") as span:
            span.set_attribute("cptr.task.id", "t-001")
