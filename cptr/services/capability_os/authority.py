"""Lease-based authority broker for Capability OS.

Generation never grants authority. This module intersects a workload request
with an owner/task policy and persists only the resulting short-lived lease.
"""

from __future__ import annotations

import fnmatch
import inspect
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from cptr.services.capability_os.contracts import CapabilityRequest
from cptr.services.capability_os.store import SqlCapabilityOsStore


class AuthorityDenied(PermissionError):
    pass


_AUTHORITY_CRITICAL_PREFIXES = (
    "auth.",
    "credential.",
    "credentials.",
    "policy.",
    "production.",
    "sandbox.",
    "secret.",
)
_AUTHORITY_CRITICAL_ACTIONS = {
    "filesystem.delete",
    "host.privileged",
    "runtime.privileged",
}


def _critical(action: str) -> bool:
    return action in _AUTHORITY_CRITICAL_ACTIONS or action.startswith(_AUTHORITY_CRITICAL_PREFIXES)


def _resource_matches(pattern: str, value: str) -> bool:
    return pattern == value or fnmatch.fnmatchcase(value, pattern)


def _constraints_within(requested: dict[str, Any], allowed: dict[str, Any]) -> bool:
    if not allowed:
        return not requested
    for key, value in requested.items():
        if key not in allowed:
            return False
        ceiling = allowed[key]
        if isinstance(value, (int, float)) and isinstance(ceiling, (int, float)):
            if value > ceiling:
                return False
        elif value != ceiling:
            return False
    return True


def _covered(requested: CapabilityRequest, allowed: CapabilityRequest) -> bool:
    return (
        requested.action == allowed.action
        and _resource_matches(allowed.resource, requested.resource)
        and _constraints_within(requested.constraints, allowed.constraints)
    )


def permission_covers(allowed: CapabilityRequest, requested: CapabilityRequest) -> bool:
    """Return whether one leased/policy permission covers a requested permission."""
    return _covered(requested, allowed)


@dataclass(frozen=True)
class TaskAuthorityPolicy:
    allowed: tuple[CapabilityRequest, ...]
    forbidden: tuple[CapabilityRequest, ...] = ()
    max_lease_ms: int = 30_000
    max_calls: int | None = None
    max_bytes_read: int | None = None
    max_bytes_written: int | None = None
    max_network_bytes: int | None = None
    outbound_network: str = "deny"
    network_destinations: tuple[str, ...] = ()
    credential_names: tuple[str, ...] = ()
    resource_limits: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_lease_ms <= 0:
            raise ValueError("max_lease_ms must be positive")
        if self.outbound_network not in {"deny", "allow-list"}:
            raise ValueError("outbound_network must be deny or allow-list")
        if not isinstance(self.resource_limits, dict):
            raise TypeError("resource_limits must be an object")


@dataclass(frozen=True)
class LeaseRequest:
    task_id: str
    workload_id: str
    artifact_digest: str
    permissions: tuple[CapabilityRequest, ...]
    runtime_profile: str
    requested_lease_ms: int
    parent_lease_id: str | None = None
    resource_limits: dict[str, Any] = field(default_factory=dict)
    network_destinations: tuple[str, ...] = ()
    credential_names: tuple[str, ...] = ()
    execution_context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("task_id", "workload_id", "artifact_digest", "runtime_profile"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")
        if self.requested_lease_ms <= 0:
            raise ValueError("requested_lease_ms must be positive")
        if not self.permissions:
            raise ValueError("a capability lease must request at least one permission")
        if not isinstance(self.execution_context, dict):
            raise TypeError("execution_context must be an object")


@dataclass(frozen=True)
class CapabilityLease:
    lease_id: str
    task_id: str
    workload_id: str
    artifact_digest: str
    permissions: tuple[CapabilityRequest, ...]
    resource_limits: dict[str, Any]
    network: dict[str, Any]
    credentials: dict[str, Any]
    approval_id: str | None
    parent_lease_id: str | None
    policy_decision_id: str
    runtime_profile: str
    issued_at_ms: int
    expires_at_ms: int
    status: str
    execution_context: dict[str, Any] = field(default_factory=dict)


ApprovalVerifier = Callable[[str, LeaseRequest, tuple[CapabilityRequest, ...]], Any]


class AuthorityBroker:
    def __init__(self, *, store: SqlCapabilityOsStore, clock_ms, approval_verifier: ApprovalVerifier | None = None) -> None:
        self._store = store
        self._clock_ms = clock_ms
        self._approval_verifier = approval_verifier

    async def _verify_approval(
        self,
        approval_id: str | None,
        request: LeaseRequest,
        critical_permissions: tuple[CapabilityRequest, ...],
    ) -> None:
        if not approval_id:
            if critical_permissions:
                raise AuthorityDenied("authority-critical permission requires a verified explicit approval")
            return
        if self._approval_verifier is None:
            raise AuthorityDenied("approval cannot be verified by the server")
        verified = self._approval_verifier(approval_id, request, critical_permissions)
        if inspect.isawaitable(verified):
            verified = await verified
        if verified is not True:
            raise AuthorityDenied("authority-critical approval is invalid or does not match the request")

    async def issue(
        self,
        request: LeaseRequest,
        *,
        policy: TaskAuthorityPolicy,
        approval_id: str | None = None,
    ) -> CapabilityLease:
        if not await self._store.artifact_exists(request.artifact_digest):
            raise AuthorityDenied("lease subject artifact is not registered")

        critical_permissions: list[CapabilityRequest] = []
        for permission in request.permissions:
            if any(_covered(permission, forbidden) for forbidden in policy.forbidden):
                raise AuthorityDenied("requested permission is explicitly forbidden")
            if not any(_covered(permission, allowed) for allowed in policy.allowed):
                raise AuthorityDenied("requested permission is outside task policy")
            if _critical(permission.action):
                critical_permissions.append(permission)

        await self._verify_approval(approval_id, request, tuple(critical_permissions))

        if request.credential_names:
            unknown_credentials = set(request.credential_names) - set(policy.credential_names)
            if unknown_credentials:
                raise AuthorityDenied("requested credential is outside task policy")
        if request.network_destinations:
            if policy.outbound_network != "allow-list":
                raise AuthorityDenied("outbound network is denied by task policy")
            unknown_destinations = set(request.network_destinations) - set(policy.network_destinations)
            if unknown_destinations:
                raise AuthorityDenied("requested network destination is outside task policy")

        issued_at = int(self._clock_ms())
        expires_at = issued_at + min(int(request.requested_lease_ms), int(policy.max_lease_ms))
        limits = dict(policy.resource_limits)
        for key, value in {
            "maxCalls": policy.max_calls,
            "maxBytesRead": policy.max_bytes_read,
            "maxBytesWritten": policy.max_bytes_written,
            "maxNetworkBytes": policy.max_network_bytes,
        }.items():
            if value is not None:
                limits[key] = value
        if not _constraints_within(dict(request.resource_limits), limits):
            raise AuthorityDenied("requested resource limit exceeds task policy")
        limits.update(dict(request.resource_limits))
        decision_id = f"policy_{uuid.uuid4().hex}"
        row = await self._store.create_lease(
            task_id=request.task_id,
            workload_id=request.workload_id,
            artifact_digest=request.artifact_digest,
            permissions=[item.to_dict() for item in request.permissions],
            resource_limits=limits,
            network={
                "outbound": policy.outbound_network,
                "destinations": list(request.network_destinations),
            },
            credentials={"logicalNames": list(request.credential_names)},
            approval_id=approval_id,
            parent_lease_id=request.parent_lease_id,
            policy_decision_id=decision_id,
            attestation_requirements={
                "artifactDigest": request.artifact_digest,
                "runtimeProfile": request.runtime_profile,
                "executionContext": dict(request.execution_context),
            },
            runtime_profile=request.runtime_profile,
            issued_at_ms=issued_at,
            expires_at_ms=expires_at,
        )
        return self._lease_from_row(row)

    async def require_active(
        self,
        lease_id: str,
        *,
        task_id: str,
        artifact_digest: str,
        required_permissions: tuple[CapabilityRequest, ...] = (),
        runtime_profile: str | None = None,
        resource_limits: dict[str, Any] | None = None,
        network_destinations: tuple[str, ...] = (),
        credential_names: tuple[str, ...] = (),
        execution_context: dict[str, Any] | None = None,
    ) -> CapabilityLease:
        row = await self._store.get_lease(lease_id)
        now_ms = int(self._clock_ms())
        if row is None:
            raise AuthorityDenied("capability lease not found")
        if row.status != "active" or int(row.expires_at_ms) <= now_ms:
            raise AuthorityDenied("capability lease is inactive or expired")
        if row.task_id != task_id:
            raise AuthorityDenied("capability lease belongs to another task")
        if row.artifact_digest != artifact_digest:
            raise AuthorityDenied("capability lease belongs to another artifact")
        lease = self._lease_from_row(row)
        if required_permissions:
            for required in required_permissions:
                if not any(_covered(required, allowed) for allowed in lease.permissions):
                    raise AuthorityDenied("capability lease does not cover required permission")
        if runtime_profile is not None and lease.runtime_profile != runtime_profile:
            raise AuthorityDenied("capability lease runtime profile mismatch")
        requested_limits = dict(resource_limits or {})
        if requested_limits and not _constraints_within(requested_limits, lease.resource_limits):
            raise AuthorityDenied("capability lease does not cover requested resource limits")
        if network_destinations:
            if lease.network.get("outbound") != "allow-list":
                raise AuthorityDenied("capability lease does not allow outbound network")
            allowed_destinations = set(lease.network.get("destinations") or ())
            if set(network_destinations) - allowed_destinations:
                raise AuthorityDenied("capability lease does not cover requested network destination")
        if credential_names:
            allowed_credentials = set(lease.credentials.get("logicalNames") or ())
            if set(credential_names) - allowed_credentials:
                raise AuthorityDenied("capability lease does not cover requested credential")
        expected_context = dict(execution_context or {})
        if expected_context:
            if any(lease.execution_context.get(key) != value for key, value in expected_context.items()):
                raise AuthorityDenied("capability lease execution context mismatch")
        return lease

    async def revoke(self, lease_id: str) -> bool:
        return await self._store.revoke_lease(lease_id, now_ms=int(self._clock_ms()))

    async def close_task(self, task_id: str) -> int:
        return await self._store.revoke_task_leases(task_id, now_ms=int(self._clock_ms()))

    @staticmethod
    def _lease_from_row(row) -> CapabilityLease:
        return CapabilityLease(
            lease_id=row.lease_id,
            task_id=row.task_id,
            workload_id=row.workload_id,
            artifact_digest=row.artifact_digest,
            permissions=tuple(CapabilityRequest.from_dict(item) for item in row.permissions or []),
            resource_limits=dict(row.resource_limits or {}),
            network=dict(row.network or {}),
            credentials=dict(row.credentials or {}),
            approval_id=row.approval_id,
            parent_lease_id=row.parent_lease_id,
            policy_decision_id=row.policy_decision_id,
            runtime_profile=row.runtime_profile,
            issued_at_ms=int(row.issued_at_ms),
            expires_at_ms=int(row.expires_at_ms),
            status=row.status,
            execution_context=dict((row.attestation_requirements or {}).get("executionContext") or {}),
        )
