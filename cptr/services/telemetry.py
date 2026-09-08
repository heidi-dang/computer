"""Optional, bounded OpenTelemetry tracing for CPTR.

Telemetry is disabled by default. When enabled, only an explicit allowlist of
low-cardinality operational attributes may be exported. User content, paths,
claims, credentials, prompts, inputs, outputs, and bearer material are never
accepted as span attributes by this module.
"""

from __future__ import annotations

from contextlib import nullcontext
from threading import Lock
from typing import Any, ContextManager

from cptr.env import OTEL_ENABLED, OTEL_SERVICE_NAME

_ALLOWED_ATTRIBUTES = frozenset(
    {
        "cptr.operation",
        "cptr.component",
        "cptr.artifact.kind",
        "cptr.artifact.state",
        "cptr.runtime.class",
        "cptr.transport",
        "cptr.status",
        "cptr.automatic_lease",
        "cptr.verification_passed",
        "http.request.method",
        "http.response.status_code",
    }
)
_ALLOWED_SCALARS = (bool, int, float, str)


def sanitize_attributes(attributes: dict[str, Any] | None) -> dict[str, bool | int | float | str]:
    """Return only bounded, explicitly approved telemetry attributes."""
    if not attributes:
        return {}
    safe: dict[str, bool | int | float | str] = {}
    for raw_key, value in attributes.items():
        key = str(raw_key)
        if key not in _ALLOWED_ATTRIBUTES or not isinstance(value, _ALLOWED_SCALARS):
            continue
        if isinstance(value, str):
            value = value[:160]
        safe[key] = value
    return safe


class CptrTelemetry:
    """Small optional facade so OpenTelemetry can never become a hard dependency."""

    def __init__(self, *, enabled: bool | None = None, service_name: str | None = None) -> None:
        self.enabled = OTEL_ENABLED if enabled is None else bool(enabled)
        self.service_name = (service_name or OTEL_SERVICE_NAME).strip() or "cptr"
        self._configured = False
        self._provider = None
        self._tracer = None
        self._lock = Lock()

    @property
    def configured(self) -> bool:
        return self._configured

    def configure(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._configured:
                return True
            try:
                from opentelemetry import trace
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
            except ImportError:
                # Optional dependency is intentionally fail-closed to no telemetry.
                return False

            provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.name": self.service_name,
                        "service.namespace": "cptr",
                    }
                )
            )
            # OTLPSpanExporter consumes the standard OTEL_EXPORTER_OTLP* variables.
            # CPTR never reads, logs, persists, or returns exporter headers/tokens.
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)
            self._provider = provider
            self._tracer = trace.get_tracer("cptr", "1")
            self._configured = True
            return True

    def span(
        self,
        name: str,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> ContextManager[Any]:
        if not self._configured or self._tracer is None:
            return nullcontext()
        safe_name = str(name).strip()[:120] or "cptr.operation"
        return self._tracer.start_as_current_span(
            safe_name,
            attributes=sanitize_attributes(attributes),
        )

    def shutdown(self) -> None:
        provider = self._provider
        self._provider = None
        self._tracer = None
        self._configured = False
        if provider is not None:
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                shutdown()


telemetry = CptrTelemetry()
