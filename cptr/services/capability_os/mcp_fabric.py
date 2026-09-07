"""Ephemeral MCP acquisition, qualification, projection, and release lifecycle."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum

from cptr.services.capability_os.authority import AuthorityBroker, CapabilityLease, permission_covers
from cptr.services.capability_os.contracts import CapabilityRequest
from cptr.services.capability_os.store import SqlCapabilityOsStore


class AcquisitionState(str, Enum):
    DISCOVERED = "discovered"
    QUALIFIED = "qualified"
    QUARANTINED = "quarantined"
    LEASED = "leased"
    MOUNTED = "mounted"
    RELEASED = "released"


@dataclass(frozen=True)
class AcquisitionGoal:
    task_id: str
    goal: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    forbidden: tuple[str, ...]
    data_classification: str

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.goal.strip():
            raise ValueError("acquisition task and goal must not be blank")
        if not self.required:
            raise ValueError("acquisition goal must require at least one tool")


@dataclass(frozen=True)
class McpCandidate:
    server_id: str
    version: str
    digest: str
    transport_kind: str
    tools: tuple[str, ...]
    permissions: tuple[CapabilityRequest, ...]
    identity_ok: bool
    auth_ok: bool
    sandboxable: bool
    hard_denies: tuple[str, ...]
    successes: int = 0
    failures: int = 0

    def __post_init__(self) -> None:
        if not self.server_id.strip() or not self.version.strip() or not self.digest.strip():
            raise ValueError("MCP candidate identity must not be blank")
        if self.successes < 0 or self.failures < 0:
            raise ValueError("MCP reliability counts must be non-negative")


@dataclass(frozen=True)
class McpQualification:
    candidate: McpCandidate
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class McpMount:
    mount_id: str
    task_id: str
    server_id: str
    version: str
    digest: str
    state: AcquisitionState
    lease_id: str
    projected_tools: tuple[str, ...]
    transport_kind: str


def beta_lower_bound(*, successes: int, failures: int, z: float = 1.96) -> float:
    """Conservative lower bound for a Beta(1+s,1+f) reliability posterior.

    CPTR deliberately avoids a heavy scientific dependency in the control plane;
    this uses a bounded normal approximation to the Beta posterior. The stored
    sufficient statistics remain exact and can later be evaluated with an exact
    quantile implementation without changing persistence.
    """

    if successes < 0 or failures < 0:
        raise ValueError("success/failure counts must be non-negative")
    alpha = 1.0 + float(successes)
    beta = 1.0 + float(failures)
    total = alpha + beta
    mean = alpha / total
    variance = (alpha * beta) / ((total * total) * (total + 1.0))
    return max(0.0, min(1.0, mean - float(z) * math.sqrt(variance)))


class McpFabric:
    def __init__(
        self,
        *,
        store: SqlCapabilityOsStore,
        authority: AuthorityBroker,
        credential_broker=None,
        clock_ms=lambda: int(time.time() * 1000),
    ) -> None:
        self._store = store
        self._authority = authority
        self._credential_broker = credential_broker
        self._clock_ms = clock_ms

    def qualify(self, candidate: McpCandidate) -> McpQualification:
        reasons: list[str] = []
        if not candidate.identity_ok:
            reasons.append("identity")
        if not candidate.auth_ok:
            reasons.append("auth")
        if not candidate.sandboxable:
            reasons.append("sandbox")
        if candidate.hard_denies:
            reasons.extend(f"hard-deny:{item}" for item in candidate.hard_denies)
        return McpQualification(
            candidate=candidate,
            eligible=not reasons,
            reasons=tuple(reasons),
        )

    def utility_score(self, candidate: McpCandidate, *, relevance: float = 1.0) -> float:
        if not self.qualify(candidate).eligible:
            return float("-inf")
        reliability = beta_lower_bound(
            successes=candidate.successes,
            failures=candidate.failures,
        )
        authority_cost = min(1.0, len(candidate.permissions) / 10.0)
        return (0.55 * max(0.0, min(1.0, relevance))) + (0.35 * reliability) - (
            0.10 * authority_cost
        )

    async def mount(
        self,
        *,
        goal: AcquisitionGoal,
        qualification: McpQualification,
        lease: CapabilityLease,
    ) -> McpMount:
        candidate = qualification.candidate
        if not qualification.eligible:
            raise PermissionError("MCP candidate did not pass hard qualification")
        if lease.status != "active":
            raise PermissionError("MCP mount requires an active lease")
        if lease.task_id != goal.task_id:
            raise PermissionError("MCP lease belongs to a different task")
        if lease.artifact_digest != candidate.digest:
            raise PermissionError("MCP lease artifact does not match candidate digest")
        if lease.runtime_profile != "remote-mcp":
            raise PermissionError("MCP lease has the wrong runtime profile")
        for required in candidate.permissions:
            if not any(permission_covers(allowed, required) for allowed in lease.permissions):
                raise PermissionError("MCP lease lacks candidate permission")

        tools = set(candidate.tools)
        required = tuple(dict.fromkeys(item.strip() for item in goal.required if item.strip()))
        missing = [item for item in required if item not in tools]
        if missing:
            raise ValueError(f"MCP candidate is missing required tool: {missing[0]}")
        forbidden = {item.strip() for item in goal.forbidden if item.strip()}
        if any(item in forbidden for item in required):
            raise PermissionError("acquisition goal requires a forbidden MCP tool")
        projected = tuple(item for item in required if item not in forbidden)

        row = await self._store.create_mcp_mount(
            task_id=goal.task_id,
            server_id=candidate.server_id,
            version=candidate.version,
            digest=candidate.digest,
            lease_id=lease.lease_id,
            projected_tools=projected,
            transport_kind=candidate.transport_kind,
            now_ms=int(self._clock_ms()),
        )
        return self._mount_from_row(row)

    async def release(self, mount_id: str, *, task_id: str | None = None) -> bool:
        row = await self._store.get_mcp_mount(mount_id)
        if row is None:
            return False
        if task_id is not None and row.task_id != task_id:
            raise PermissionError("MCP mount belongs to a different task")
        now_ms = int(self._clock_ms())
        released = await self._store.release_mcp_mount(mount_id, now_ms=now_ms)
        if released:
            if self._credential_broker is not None:
                await self._credential_broker.revoke_lease(row.lease_id)
            await self._authority.revoke(row.lease_id)
        return released

    async def close_task(self, task_id: str) -> dict[str, int]:
        now_ms = int(self._clock_ms())
        released_mounts = await self._store.release_task_mounts(task_id, now_ms=now_ms)
        if self._credential_broker is not None:
            await self._credential_broker.revoke_task(task_id)
        revoked_leases = await self._authority.close_task(task_id)
        remaining_mounts = await self._store.list_active_mounts(task_id)
        remaining_leases = await self._store.list_active_leases(task_id, now_ms=now_ms)
        if remaining_mounts or remaining_leases:
            raise RuntimeError("task-close zero-authority invariant failed")
        return {"released_mounts": released_mounts, "revoked_leases": revoked_leases}

    @staticmethod
    def _mount_from_row(row) -> McpMount:
        return McpMount(
            mount_id=row.mount_id,
            task_id=row.task_id,
            server_id=row.server_id,
            version=row.version,
            digest=row.digest,
            state=AcquisitionState(row.state),
            lease_id=row.lease_id,
            projected_tools=tuple(row.projected_tools or []),
            transport_kind=row.transport_kind,
        )
