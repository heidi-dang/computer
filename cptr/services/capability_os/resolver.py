"""Capability OS resolver for reusable artefacts and missing-ability detection."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from cptr.services.capability_os.contracts import ArtifactState, CapabilityRequest
from cptr.services.capability_os.store import SqlCapabilityOsStore


_REUSABLE_STATES = {
    ArtifactState.QUALIFIED.value,
    ArtifactState.LEARNED.value,
    ArtifactState.CERTIFIED.value,
    ArtifactState.CORE.value,
}
_STATE_SCORE = {
    ArtifactState.EPHEMERAL.value: 0.35,
    ArtifactState.QUALIFIED.value: 0.65,
    ArtifactState.LEARNED.value: 0.75,
    ArtifactState.CERTIFIED.value: 0.9,
    ArtifactState.CORE.value: 1.0,
}


def _covers(offered: CapabilityRequest, requested: CapabilityRequest) -> bool:
    return offered.action == requested.action and (
        offered.resource == requested.resource
        or fnmatch.fnmatchcase(requested.resource, offered.resource)
    )


def _overlaps(left: CapabilityRequest, right: CapabilityRequest) -> bool:
    if left.action != right.action:
        return False
    return (
        left.resource == right.resource
        or fnmatch.fnmatchcase(left.resource, right.resource)
        or fnmatch.fnmatchcase(right.resource, left.resource)
    )


def _capabilities(spec: dict) -> tuple[CapabilityRequest, ...]:
    raw = spec.get("requestedCapabilities")
    if not isinstance(raw, list):
        raw = spec.get("permissions")
    if not isinstance(raw, list):
        return ()
    result: list[CapabilityRequest] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            result.append(CapabilityRequest.from_dict(item))
        except (TypeError, ValueError):
            continue
    return tuple(result)


@dataclass(frozen=True)
class ResolutionGoal:
    task_id: str
    required: tuple[CapabilityRequest, ...]
    optional: tuple[CapabilityRequest, ...]
    forbidden: tuple[CapabilityRequest, ...]

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("resolution task id must not be blank")
        if not self.required:
            raise ValueError("resolution goal must require at least one effect")


@dataclass(frozen=True)
class ResolutionCandidate:
    artifact_id: str
    version: str
    kind: str
    content_digest: str
    state: str
    origin: str
    task_origin: str | None
    capabilities: tuple[CapabilityRequest, ...]
    score: float


@dataclass(frozen=True)
class ResolutionResult:
    candidates: tuple[ResolutionCandidate, ...]
    missing: tuple[CapabilityRequest, ...]
    acquisition_modes: tuple[str, ...]


class CapabilityResolver:
    def __init__(self, *, store: SqlCapabilityOsStore) -> None:
        self._store = store

    async def resolve(
        self, goal: ResolutionGoal, *, user_id: str | None = None
    ) -> ResolutionResult:
        rows = await self._store.list_artifacts(
            user_id=user_id, include_global=True, limit=1000
        )
        candidates: list[ResolutionCandidate] = []
        for row in rows:
            if row.state == ArtifactState.RETIRED.value:
                continue
            if row.state == ArtifactState.EPHEMERAL.value and row.task_origin != goal.task_id:
                continue
            if row.state != ArtifactState.EPHEMERAL.value and row.state not in _REUSABLE_STATES:
                continue
            offered = _capabilities(dict(row.spec or {}))
            if not offered:
                continue
            if any(
                _overlaps(capability, forbidden)
                for capability in offered
                for forbidden in goal.forbidden
            ):
                continue
            if not all(
                any(_covers(capability, required) for capability in offered)
                for required in goal.required
            ):
                continue

            required_exact = sum(
                1
                for required in goal.required
                if any(
                    capability.action == required.action
                    and capability.resource == required.resource
                    for capability in offered
                )
            )
            optional_coverage = sum(
                1
                for optional in goal.optional
                if any(_covers(capability, optional) for capability in offered)
            )
            required_score = required_exact / max(1, len(goal.required))
            optional_score = optional_coverage / max(1, len(goal.optional)) if goal.optional else 0.0
            authority_cost = max(0, len(offered) - len(goal.required)) / max(1, len(offered))
            score = (
                0.45 * _STATE_SCORE.get(row.state, 0.0)
                + 0.40 * required_score
                + 0.10 * optional_score
                - 0.05 * authority_cost
            )
            candidates.append(
                ResolutionCandidate(
                    artifact_id=row.artifact_id,
                    version=row.version,
                    kind=row.kind,
                    content_digest=row.content_digest,
                    state=row.state,
                    origin=row.origin,
                    task_origin=row.task_origin,
                    capabilities=offered,
                    score=score,
                )
            )

        candidates.sort(
            key=lambda item: (-item.score, item.artifact_id, item.version, item.content_digest)
        )
        if candidates:
            missing: tuple[CapabilityRequest, ...] = ()
            acquisition_modes: tuple[str, ...] = ()
        else:
            missing = goal.required
            acquisition_modes = ("forge", "mcp")
        return ResolutionResult(
            candidates=tuple(candidates),
            missing=missing,
            acquisition_modes=acquisition_modes,
        )
