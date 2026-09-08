import sys
import types
import unittest
from unittest.mock import patch

from cptr.services.telemetry import CptrTelemetry, sanitize_attributes


class TelemetrySanitizationTests(unittest.TestCase):
    def test_only_allowlisted_low_cardinality_attributes_survive(self):
        safe = sanitize_attributes(
            {
                "cptr.operation": "execute",
                "cptr.component": "capability-os",
                "cptr.status": "complete",
                "cptr.automatic_lease": True,
                "task_id": "task-secret",
                "artifact_digest": "sha256:" + "a" * 64,
                "prompt": "sensitive prompt",
                "credential_name": "prod-token",
                "claims": {"secret": "value"},
            }
        )
        self.assertEqual(
            safe,
            {
                "cptr.operation": "execute",
                "cptr.component": "capability-os",
                "cptr.status": "complete",
                "cptr.automatic_lease": True,
            },
        )

    def test_string_attributes_are_bounded(self):
        safe = sanitize_attributes({"cptr.status": "x" * 500})
        self.assertEqual(len(safe["cptr.status"]), 160)


class TelemetryLifecycleTests(unittest.TestCase):
    def test_disabled_telemetry_is_a_noop(self):
        telemetry = CptrTelemetry(enabled=False)
        self.assertFalse(telemetry.configure())
        self.assertFalse(telemetry.configured)
        with telemetry.span("cptr.capability_os.execute", attributes={"cptr.operation": "execute"}):
            pass
        telemetry.shutdown()

    def test_enabled_telemetry_uses_otlp_exporter_without_exposing_configuration(self):
        recorded = {"providers": [], "processors": [], "spans": [], "shutdown": 0}

        class FakeSpan:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeTracer:
            def start_as_current_span(self, name, *, attributes):
                recorded["spans"].append((name, attributes))
                return FakeSpan()

        class FakeTraceModule(types.ModuleType):
            def set_tracer_provider(self, provider):
                recorded["providers"].append(provider)

            def get_tracer(self, name, version):
                self.last_get = (name, version)
                return FakeTracer()

        class FakeResource:
            @staticmethod
            def create(attributes):
                return dict(attributes)

        class FakeProvider:
            def __init__(self, *, resource):
                self.resource = resource

            def add_span_processor(self, processor):
                recorded["processors"].append(processor)

            def shutdown(self):
                recorded["shutdown"] += 1

        class FakeExporter:
            def __init__(self):
                self.created = True

        class FakeProcessor:
            def __init__(self, exporter):
                self.exporter = exporter

        otel = types.ModuleType("opentelemetry")
        trace = FakeTraceModule("opentelemetry.trace")
        otel.trace = trace
        exporter_pkg = types.ModuleType("opentelemetry.exporter")
        exporter_otlp = types.ModuleType("opentelemetry.exporter.otlp")
        exporter_proto = types.ModuleType("opentelemetry.exporter.otlp.proto")
        exporter_http = types.ModuleType("opentelemetry.exporter.otlp.proto.http")
        exporter_trace = types.ModuleType("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        exporter_trace.OTLPSpanExporter = FakeExporter
        sdk = types.ModuleType("opentelemetry.sdk")
        resources = types.ModuleType("opentelemetry.sdk.resources")
        resources.Resource = FakeResource
        sdk_trace = types.ModuleType("opentelemetry.sdk.trace")
        sdk_trace.TracerProvider = FakeProvider
        sdk_export = types.ModuleType("opentelemetry.sdk.trace.export")
        sdk_export.BatchSpanProcessor = FakeProcessor

        fake_modules = {
            "opentelemetry": otel,
            "opentelemetry.trace": trace,
            "opentelemetry.exporter": exporter_pkg,
            "opentelemetry.exporter.otlp": exporter_otlp,
            "opentelemetry.exporter.otlp.proto": exporter_proto,
            "opentelemetry.exporter.otlp.proto.http": exporter_http,
            "opentelemetry.exporter.otlp.proto.http.trace_exporter": exporter_trace,
            "opentelemetry.sdk": sdk,
            "opentelemetry.sdk.resources": resources,
            "opentelemetry.sdk.trace": sdk_trace,
            "opentelemetry.sdk.trace.export": sdk_export,
        }
        with patch.dict(sys.modules, fake_modules, clear=False):
            telemetry = CptrTelemetry(enabled=True, service_name="cptr-test")
            self.assertTrue(telemetry.configure())
            self.assertTrue(telemetry.configure())
            with telemetry.span(
                "cptr.capability_os.execute",
                attributes={
                    "cptr.operation": "execute",
                    "task_id": "must-not-export",
                },
            ):
                pass
            telemetry.shutdown()

        self.assertEqual(len(recorded["providers"]), 1)
        self.assertEqual(recorded["providers"][0].resource["service.name"], "cptr-test")
        self.assertEqual(len(recorded["processors"]), 1)
        self.assertEqual(
            recorded["spans"],
            [("cptr.capability_os.execute", {"cptr.operation": "execute"})],
        )
        self.assertEqual(recorded["shutdown"], 1)


if __name__ == "__main__":
    unittest.main()
