"""Server-owned standing authority policy selection.

Public Capability OS requests cannot submit or widen these policies. Operators
construct rules out of band; absence or ambiguity always denies authority.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from cptr.services.capability_os.authority import AuthorityDenied, TaskAuthorityPolicy
from cptr.services.capability_os.contracts import CapabilityRequest


class AuthorityPolicyProvider(Protocol):
    async def resolve(self, *, task, artifact_digest: str, workload_id: str) -> TaskAuthorityPolicy | None: ...


class DenyAllAuthorityPolicyProvider:
    async def resolve(self, *, task, artifact_digest: str, workload_id: str) -> None:
        return None


@dataclass(frozen=True)
class StandingAuthorityRule:
    policy: TaskAuthorityPolicy
    user_id: str | None = None
    workspace_id: str | None = None
    task_source: str | None = None
    workload_pattern: str | None = None
    artifact_pattern: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "user_id",
            "workspace_id",
            "task_source",
            "workload_pattern",
            "artifact_pattern",
        ):
            value = getattr(self, name)
            if value is not None and not str(value).strip():
                raise ValueError(f"{name} must not be blank when supplied")

    def matches(self, task, *, artifact_digest: str = "", workload_id: str = "") -> bool:
        return (
            (self.user_id is None or self.user_id == task.user_id)
            and (self.workspace_id is None or self.workspace_id == task.workspace_id)
            and (self.task_source is None or self.task_source == task.source)
            and (
                self.workload_pattern is None
                or fnmatch.fnmatchcase(str(workload_id), self.workload_pattern)
            )
            and (
                self.artifact_pattern is None
                or fnmatch.fnmatchcase(str(artifact_digest), self.artifact_pattern)
            )
        )

    @property
    def specificity(self) -> int:
        return sum(
            value is not None
            for value in (
                self.user_id,
                self.workspace_id,
                self.task_source,
                self.workload_pattern,
                self.artifact_pattern,
            )
        )


class StandingAuthorityPolicyProvider:
    def __init__(self, rules: tuple[StandingAuthorityRule, ...]) -> None:
        self._rules = tuple(rules)

    async def resolve(self, *, task, artifact_digest: str, workload_id: str) -> TaskAuthorityPolicy | None:
        matches = [
            rule
            for rule in self._rules
            if rule.matches(task, artifact_digest=artifact_digest, workload_id=workload_id)
        ]
        if not matches:
            return None
        highest = max(rule.specificity for rule in matches)
        selected = [rule for rule in matches if rule.specificity == highest]
        if len(selected) != 1:
            raise AuthorityDenied("standing authority policy is ambiguous")
        return selected[0].policy


def _optional_positive_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{field_name} must be positive")
    return parsed


def _policy_from_dict(value: Any) -> TaskAuthorityPolicy:
    if not isinstance(value, dict):
        raise ValueError("standing authority policy must be an object")
    allowed = tuple(
        item if isinstance(item, CapabilityRequest) else CapabilityRequest.from_dict(item)
        for item in value.get("allowed") or ()
    )
    if not allowed:
        raise ValueError("standing authority policy requires at least one allowed permission")
    forbidden = tuple(
        item if isinstance(item, CapabilityRequest) else CapabilityRequest.from_dict(item)
        for item in value.get("forbidden") or ()
    )
    resource_limits = value.get("resourceLimits") or {}
    if not isinstance(resource_limits, dict):
        raise ValueError("standing authority resourceLimits must be an object")
    network_destinations = tuple(
        str(item).strip() for item in value.get("networkDestinations") or () if str(item).strip()
    )
    credential_names = tuple(
        str(item).strip() for item in value.get("credentialNames") or () if str(item).strip()
    )
    return TaskAuthorityPolicy(
        allowed=allowed,
        forbidden=forbidden,
        max_lease_ms=int(value.get("maxLeaseMs") or 30_000),
        max_calls=_optional_positive_int(value.get("maxCalls"), field_name="maxCalls"),
        max_bytes_read=_optional_positive_int(value.get("maxBytesRead"), field_name="maxBytesRead"),
        max_bytes_written=_optional_positive_int(value.get("maxBytesWritten"), field_name="maxBytesWritten"),
        max_network_bytes=_optional_positive_int(value.get("maxNetworkBytes"), field_name="maxNetworkBytes"),
        outbound_network=str(value.get("outboundNetwork") or "deny"),
        network_destinations=network_destinations,
        credential_names=credential_names,
        resource_limits=dict(resource_limits),
    )


def _rule_from_dict(value: Any) -> StandingAuthorityRule | None:
    if not isinstance(value, dict) or value.get("enabled", True) is not True:
        return None
    selectors = value.get("selectors") or {}
    if not isinstance(selectors, dict):
        raise ValueError("standing authority selectors must be an object")
    unknown = set(selectors) - {
        "userId", "workspaceId", "taskSource", "workloadPattern", "artifactPattern"
    }
    if unknown:
        raise ValueError(f"unknown standing authority selector: {sorted(unknown)[0]}")
    return StandingAuthorityRule(
        policy=_policy_from_dict(value.get("policy")),
        user_id=(str(selectors["userId"]).strip() if selectors.get("userId") is not None else None),
        workspace_id=(
            str(selectors["workspaceId"]).strip()
            if selectors.get("workspaceId") is not None
            else None
        ),
        task_source=(
            str(selectors["taskSource"]).strip()
            if selectors.get("taskSource") is not None
            else None
        ),
        workload_pattern=(
            str(selectors["workloadPattern"]).strip()
            if selectors.get("workloadPattern") is not None
            else None
        ),
        artifact_pattern=(
            str(selectors["artifactPattern"]).strip()
            if selectors.get("artifactPattern") is not None
            else None
        ),
    )


class ConfigStandingAuthorityPolicyProvider:
    """Load durable operator-owned standing authority policy from Config.

    The public Capability OS API cannot write this registry. Invalid or ambiguous
    configuration fails closed instead of silently broadening authority.
    """

    def __init__(
        self,
        *,
        config_getter: Callable[[str], Awaitable[Any]] | None = None,
        config_key: str = "capability_os.authority_policies",
    ) -> None:
        self._config_getter = config_getter
        self._config_key = str(config_key).strip()
        if not self._config_key:
            raise ValueError("standing authority config key must not be blank")

    async def _get(self) -> Any:
        if self._config_getter is not None:
            return await self._config_getter(self._config_key)
        from cptr.models import Config
        return await Config.get(self._config_key)

    async def resolve(self, *, task, artifact_digest: str, workload_id: str) -> TaskAuthorityPolicy | None:
        raw = await self._get()
        if raw is None:
            return None
        if not isinstance(raw, list):
            raise AuthorityDenied("standing authority configuration is invalid")
        if len(raw) > 256:
            raise AuthorityDenied("standing authority configuration exceeds rule limit")
        try:
            rules = tuple(rule for item in raw if (rule := _rule_from_dict(item)) is not None)
        except (TypeError, ValueError) as exc:
            raise AuthorityDenied("standing authority configuration is invalid") from exc
        return await StandingAuthorityPolicyProvider(rules).resolve(
            task=task,
            artifact_digest=artifact_digest,
            workload_id=workload_id,
        )


class CompositeAuthorityPolicyProvider:
    """Compose independent server-owned providers without implicit precedence."""

    def __init__(self, providers: tuple[AuthorityPolicyProvider, ...]) -> None:
        self._providers = tuple(providers)

    async def resolve(self, *, task, artifact_digest: str, workload_id: str) -> TaskAuthorityPolicy | None:
        matches: list[TaskAuthorityPolicy] = []
        for provider in self._providers:
            policy = await provider.resolve(
                task=task,
                artifact_digest=artifact_digest,
                workload_id=workload_id,
            )
            if policy is not None:
                matches.append(policy)
        if len(matches) > 1:
            raise AuthorityDenied("multiple standing authority providers match the same workload")
        return matches[0] if matches else None


class SafeIsolationAuthorityPolicyProvider:
    """Server-owned automatic policy for network-denied isolated Tool build/run operations."""
    def __init__(self, *, max_cpu_millis: int, max_memory_mib: int, max_disk_mib: int,
                 max_pids: int, max_wall_time_ms: int, max_output_bytes: int) -> None:
        values = (max_cpu_millis, max_memory_mib, max_disk_mib, max_pids, max_wall_time_ms, max_output_bytes)
        if any(int(value) <= 0 for value in values):
            raise ValueError("safe isolation resource ceilings must be positive")
        self.resource_limits = {
            "cpuMillis": int(max_cpu_millis),
            "memoryMiB": int(max_memory_mib),
            "diskMiB": int(max_disk_mib),
            "pids": int(max_pids),
            "wallTimeMs": int(max_wall_time_ms),
            "maxOutputBytes": int(max_output_bytes),
        }

    async def resolve(self, *, task, artifact_digest: str, workload_id: str) -> TaskAuthorityPolicy | None:
        workload = str(workload_id)
        if workload.startswith("tool-build:"):
            action = "runtime.build"
        elif workload.startswith("tool-run:"):
            action = "runtime.run"
        else:
            return None
        return TaskAuthorityPolicy(
            allowed=(CapabilityRequest(action, f"artifact:{artifact_digest}"),),
            max_lease_ms=min(600_000, self.resource_limits["wallTimeMs"] + 5_000),
            max_calls=1,
            outbound_network="deny",
            resource_limits=dict(self.resource_limits),
        )
