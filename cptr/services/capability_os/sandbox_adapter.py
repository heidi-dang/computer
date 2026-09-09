"""Backend adapters for the root-owned Capability OS sandbox broker."""
from __future__ import annotations

from typing import Any

from cptr.services.capability_os.authority import permission_covers
from cptr.services.capability_os.contracts import CapabilityRequest
from cptr.services.capability_os.runtime import RuntimeClass, RuntimeUnavailable
from cptr.services.capability_os.sandbox_broker import (
    BrokerRuntimeUnavailable, BrokerUnavailable, SandboxBrokerClient, SandboxRequest,
)
from cptr.services.capability_os.vm import ActionResult


_PROFILE_BY_SUFFIX = {
    ".py": "python", ".js": "node", ".mjs": "node", ".cjs": "node",
    ".sh": "shell", ".rs": "rust", ".go": "go", ".wat": "wasm",
}
_ALLOWED_PROFILES = frozenset({"python", "node", "shell", "rust", "go", "wasm"})


def _resource_subset(requested: dict[str, Any], allowed: dict[str, Any]) -> bool:
    for key, value in requested.items():
        if key not in allowed:
            return False
        ceiling = allowed[key]
        if isinstance(value, (int, float)) and isinstance(ceiling, (int, float)):
            if value < 0 or value > ceiling:
                return False
        elif value != ceiling:
            return False
    return True


def _profile(artifact) -> str:
    runtime = dict(artifact.spec.get("runtime") or {})
    declared = str(runtime.get("language") or "").strip().lower()
    if declared:
        if declared not in _ALLOWED_PROFILES:
            raise ValueError("unsupported sandbox tool language")
        return declared
    entrypoint = str(runtime.get("entrypoint") or "")
    for suffix, profile in _PROFILE_BY_SUFFIX.items():
        if entrypoint.lower().endswith(suffix):
            return profile
    raise ValueError("sandbox tool language cannot be inferred")


def _require_sandbox_lease(*, runtime_class: RuntimeClass, artifact, lease, action: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    if runtime_class is RuntimeClass.NAMESPACE_DEV:
        raise RuntimeUnavailable("namespace-dev is not an approved production broker runtime")
    if lease.artifact_digest != artifact.content_digest:
        raise PermissionError("sandbox lease does not match Tool artefact")
    if action == "runtime.build" and lease.task_id != artifact.task_origin:
        raise PermissionError("Tool Forge build lease belongs to another task")
    if getattr(lease, "runtime_profile", None) != runtime_class.value:
        raise PermissionError("sandbox lease runtime profile mismatch")
    required = CapabilityRequest(action, f"artifact:{artifact.content_digest}")
    if not any(permission_covers(allowed, required) for allowed in getattr(lease, "permissions", ())):
        raise PermissionError(f"sandbox lease lacks {action} permission")
    requested_resources = dict(artifact.spec.get("resources") or {})
    allowed_resources = dict(lease.resource_limits or {})
    if not _resource_subset(requested_resources, allowed_resources):
        raise PermissionError("sandbox resources exceed capability lease")
    runtime = dict(artifact.spec.get("runtime") or {})
    entrypoint = str(runtime.get("entrypoint") or "")
    network = dict(lease.network or {"outbound": "deny", "destinations": []})
    return requested_resources, allowed_resources, network, entrypoint


class BrokerToolBuilder:
    def __init__(self, *, client: SandboxBrokerClient) -> None:
        self.client = client

    async def __call__(self, *, runtime_class: RuntimeClass, artifact, lease) -> dict[str, Any]:
        requested_resources, _allowed_resources, network, entrypoint = _require_sandbox_lease(
            runtime_class=runtime_class, artifact=artifact, lease=lease, action="runtime.build"
        )
        timeout_ms = int(requested_resources.get("wallTimeMs") or 30_000)
        request = SandboxRequest(
            operation="build",
            task_id=lease.task_id,
            lease_id=lease.lease_id,
            artifact_digest=artifact.content_digest,
            runtime_class=runtime_class.value,
            bundle_digest=str(artifact.source_digest),
            profile=_profile(artifact),
            entrypoint=entrypoint,
            timeout_ms=timeout_ms,
            resources=requested_resources,
            network={
                "outbound": str(network.get("outbound") or "deny"),
                "destinations": list(network.get("destinations") or []),
            },
        )
        try:
            result = await self.client.invoke(request)
        except (BrokerUnavailable, BrokerRuntimeUnavailable) as exc:
            raise RuntimeUnavailable(str(exc)) from exc
        artifact_digest = str(result.get("artifactDigest") or "")
        attestation = result.get("attestation")
        if not artifact_digest.startswith("sha256:") or len(artifact_digest) != 71:
            raise ValueError("sandbox broker returned invalid build artefact digest")
        if not isinstance(attestation, dict):
            raise ValueError("sandbox broker returned invalid build attestation")
        return {"artifact_digest": artifact_digest, "attestation": dict(attestation)}


class BrokerToolRunner:
    """Run a qualified generated Tool through the broker without widening its lease."""

    def __init__(self, *, client: SandboxBrokerClient) -> None:
        self.client = client

    async def __call__(
        self,
        *,
        runtime_class: RuntimeClass,
        artifact,
        lease,
        inputs: dict[str, Any],
        timeout_ms: int,
    ) -> ActionResult:
        if not isinstance(inputs, dict):
            raise TypeError("generated Tool inputs must be an object")
        requested_resources, _allowed_resources, network, entrypoint = _require_sandbox_lease(
            runtime_class=runtime_class, artifact=artifact, lease=lease, action="runtime.run"
        )
        wall_time_ms = int(requested_resources.get("wallTimeMs") or 0)
        bounded_timeout = int(timeout_ms)
        if bounded_timeout < 100 or wall_time_ms < 100:
            raise ValueError("generated Tool timeout is outside broker bounds")
        bounded_timeout = min(bounded_timeout, wall_time_ms)
        request = SandboxRequest(
            operation="run",
            task_id=lease.task_id,
            lease_id=lease.lease_id,
            artifact_digest=artifact.content_digest,
            runtime_class=runtime_class.value,
            bundle_digest=str(artifact.source_digest),
            profile=_profile(artifact),
            entrypoint=entrypoint,
            timeout_ms=bounded_timeout,
            resources=requested_resources,
            network={
                "outbound": str(network.get("outbound") or "deny"),
                "destinations": list(network.get("destinations") or []),
            },
            inputs=dict(inputs),
        )
        try:
            result = await self.client.invoke(request)
        except (BrokerUnavailable, BrokerRuntimeUnavailable) as exc:
            raise RuntimeUnavailable(str(exc)) from exc
        artifact_digest = str(result.get("artifactDigest") or "")
        attestation = result.get("attestation")
        if not artifact_digest.startswith("sha256:") or len(artifact_digest) != 71:
            raise ValueError("sandbox broker returned invalid execution artefact digest")
        if not isinstance(attestation, dict) or "output" not in result:
            raise ValueError("sandbox broker returned invalid generated Tool result")
        return ActionResult(
            output=result["output"],
            verification_passed=True,
            metadata={
                "executionArtifactDigest": artifact_digest,
                "attestation": dict(attestation),
                "runtimeClass": runtime_class.value,
            },
        )
