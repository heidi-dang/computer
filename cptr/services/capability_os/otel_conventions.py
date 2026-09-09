"""OTel semantic conventions for the cptr.* attribute namespace.

Defines canonical span names and attribute keys for Capability OS telemetry.
All names are prefixed with 'cptr.' per the OpenTelemetry custom namespace
convention, ensuring no collision with upstream OTel semantic conventions.

Usage::

    from cptr.services.capability_os.otel_conventions import (
        CptrSpanNames, CptrAttributes, get_tracer,
    )

    tracer = get_tracer()
    with tracer.start_as_current_span(CptrSpanNames.FORGE_BUILD) as span:
        span.set_attribute(CptrAttributes.ARTIFACT_KIND, "container")
"""

from __future__ import annotations


class CptrSpanNames:
    """Canonical span names for Capability OS operations."""

    # Forge lifecycle
    FORGE_BUILD = "cptr.forge.build"
    FORGE_RUN = "cptr.forge.run"
    FORGE_PERSIST = "cptr.forge.persist"
    FORGE_DESTROY = "cptr.forge.destroy"
    FORGE_MODIFY = "cptr.forge.modify"
    FORGE_FORK = "cptr.forge.fork"

    # MCP (Model Control Plane)
    MCP_QUALIFY = "cptr.mcp.qualify"
    MCP_MOUNT = "cptr.mcp.mount"
    MCP_INVOKE = "cptr.mcp.invoke"
    MCP_RELEASE = "cptr.mcp.release"
    MCP_DISCOVER = "cptr.mcp.discover"

    # Skill lifecycle
    SKILL_MUTATE = "cptr.skill.mutate"
    SKILL_EVALUATE = "cptr.skill.evaluate"
    SKILL_PROMOTE = "cptr.skill.promote"
    SKILL_EXPERIMENT = "cptr.skill.experiment"

    # VM operations
    VM_EXECUTE = "cptr.vm.execute"
    VM_COMPILE = "cptr.vm.compile"
    VM_COMPENSATE = "cptr.vm.compensate"

    # Authority
    AUTHORITY_CHECK = "cptr.authority.check"
    AUTHORITY_LEASE = "cptr.authority.lease"

    # Evidence
    EVIDENCE_RECORD = "cptr.evidence.record"
    EVIDENCE_VERIFY = "cptr.evidence.verify"

    # Control plane
    CONTROL_INSPECT = "cptr.control.inspect"
    CONTROL_RESOLVE = "cptr.control.resolve"
    CONTROL_FORGE = "cptr.control.forge"
    CONTROL_EXECUTE = "cptr.control.execute"
    CONTROL_ACQUIRE = "cptr.control.acquire"
    CONTROL_REFLECT = "cptr.control.reflect"


class CptrAttributes:
    """Canonical span attribute keys for Capability OS telemetry."""

    # Task / lease
    TASK_ID = "cptr.task.id"
    LEASE_ID = "cptr.lease.id"
    LEASE_TTL = "cptr.lease.ttl"

    # Artifact
    ARTIFACT_DIGEST = "cptr.artifact.digest"
    ARTIFACT_KIND = "cptr.artifact.kind"
    ARTIFACT_VERSION = "cptr.artifact.version"
    ARTIFACT_RUNTIME = "cptr.artifact.runtime"

    # Experiment / variant
    EXPERIMENT_ID = "cptr.experiment.id"
    VARIANT_ID = "cptr.variant.id"
    DELTA_Q = "cptr.experiment.delta_q"
    RECOMMENDATION = "cptr.experiment.recommendation"

    # Skill
    SKILL_ID = "cptr.skill.id"
    SKILL_OPERATOR = "cptr.skill.operator"

    # MCP
    MCP_SERVER_URI = "cptr.mcp.server_uri"
    MCP_SERVER_ID = "cptr.mcp.server_id"
    MCP_REPUTATION_SCORE = "cptr.mcp.reputation_score"
    MCP_CONTENT_DIGEST = "cptr.mcp.content_digest"

    # Policy
    POLICY_DECISION = "cptr.policy.decision"
    POLICY_NAME = "cptr.policy.name"

    # Misc
    SANDBOX_CLASS = "cptr.sandbox.class"
    FORGE_OPERATION = "cptr.forge.operation"
    AUTHORITY_EFFECT = "cptr.authority.effect"
    EVIDENCE_HASH = "cptr.evidence.hash"


# ---------------------------------------------------------------------------
# Tracer factory — graceful noop when opentelemetry is not installed
# ---------------------------------------------------------------------------

class _NoopSpan:
    """Context manager returned by _NoopTracer when OTel is unavailable."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attribute(self, key, value):
        pass

    def set_status(self, status, description=""):
        pass

    def record_exception(self, exception, **kwargs):
        pass


class _NoopTracer:
    """Minimal tracer shim used when opentelemetry-api is not installed."""

    def start_as_current_span(self, name, **kwargs):
        return _NoopSpan()


def get_tracer(name="cptr.capability_os"):
    """Return an OpenTelemetry tracer, or a no-op tracer if OTel is absent.

    Args:
        name: Instrumentation scope name (default ``cptr.capability_os``).

    Returns:
        A real OTel Tracer when opentelemetry-api is installed, or a
        _NoopTracer otherwise.
    """
    try:
        from opentelemetry import trace  # type: ignore[import]
        return trace.get_tracer(name)
    except ImportError:
        return _NoopTracer()
