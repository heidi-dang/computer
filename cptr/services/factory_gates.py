"""Declarative Dark Factory verification gates and evidence validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class FactoryGateCategory(str, Enum):
    ACCEPTANCE = "acceptance"
    REPRODUCTION = "reproduction"
    REGRESSION = "regression"
    FOCUSED_TESTS = "focused_tests"
    BROADER_TESTS = "broader_tests"
    UNIT = "unit"
    INTEGRATION = "integration"
    E2E = "e2e"
    TYPECHECK = "typecheck"
    LINT = "lint"
    BUILD = "build"
    SECURITY = "security"
    ISOLATION = "isolation"
    RESOURCE = "resource"
    PERFORMANCE = "performance"
    CLEANUP_LIFECYCLE = "cleanup_lifecycle"
    ADVERSARIAL = "adversarial"
    GIT_DIFF_REVIEW = "git_diff_review"
    GIT_DIFF_CHECK = "git_diff_check"
    CI = "ci"
    RUNTIME_SMOKE = "runtime_smoke"
    LIVE_VERIFY = "live_verify"


class FactoryGateStatus(str, Enum):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceAuthority(str, Enum):
    """Authority carried by durable evidence.

    ADVISORY includes model/worker claims and research text. USER_APPROVAL can
    authorize an operation/applicability decision but cannot prove executable
    verification. MACHINE is server-observed evidence such as exit codes, Git
    state, CI conclusions, repository profiles, or runtime results.
    """

    MACHINE = "MACHINE"
    USER_APPROVAL = "USER_APPROVAL"
    ADVISORY = "ADVISORY"


@dataclass(frozen=True)
class FactoryGateSpec:
    gate_id: str
    category: FactoryGateCategory
    required: bool = True
    applicable: bool = True
    applicability_reason: str | None = None
    invalidated_by_mutation: bool = True
    acceptance_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.gate_id.strip():
            raise ValueError("gate_id must not be blank")
        if not self.applicable and not (self.applicability_reason or "").strip():
            raise ValueError("non-applicable gate spec requires an applicability reason")


@dataclass(frozen=True)
class FactoryGatePlan:
    specs: tuple[FactoryGateSpec, ...]
    acceptance_criterion_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        gate_ids = [spec.gate_id for spec in self.specs]
        if len(gate_ids) != len(set(gate_ids)):
            raise ValueError("duplicate gate id in factory gate plan")
        known_acceptance = set(self.acceptance_criterion_ids)
        referenced = {criterion for spec in self.specs for criterion in spec.acceptance_ids}
        unknown = referenced - known_acceptance
        if unknown:
            raise ValueError(
                "unknown acceptance criterion coverage: " + ", ".join(sorted(unknown))
            )

    @property
    def by_id(self) -> Mapping[str, FactoryGateSpec]:
        return MappingProxyType({spec.gate_id: spec for spec in self.specs})


@dataclass(frozen=True)
class GateEvidence:
    evidence_id: str
    digest: str
    authority: EvidenceAuthority
    revision: str | None
    fingerprint: str | None
    kind: str
    source: str


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    status: FactoryGateStatus
    evidence_ids: tuple[str, ...]
    reason: str
    evaluated_revision: str | None = None
    evaluated_fingerprint: str | None = None


def validate_gate_evidence(
    spec: FactoryGateSpec,
    result: GateResult,
    evidence_by_id: Mapping[str, GateEvidence],
    *,
    current_revision: str | None,
    current_fingerprint: str | None,
) -> list[str]:
    """Return deterministic reasons a gate result cannot be trusted.

    No model call occurs here. Passing execution gates require at least one
    current authoritative MACHINE evidence record. Explicitly non-applicable
    gates likewise require authoritative applicability evidence so a worker
    cannot drop inconvenient gates.
    """

    failures: list[str] = []
    if result.gate_id != spec.gate_id:
        failures.append(
            f"gate result id {result.gate_id!r} does not match spec {spec.gate_id!r}"
        )
        return failures

    referenced: list[GateEvidence] = []
    for evidence_id in result.evidence_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            failures.append(f"missing evidence {evidence_id!r}")
        else:
            referenced.append(evidence)

    if result.status is FactoryGateStatus.PASS:
        if not spec.applicable:
            failures.append("non-applicable gate cannot be marked PASS")
        if not result.evidence_ids:
            failures.append("PASS requires evidence")
        if not any(item.authority is EvidenceAuthority.MACHINE for item in referenced):
            failures.append("PASS requires authoritative machine evidence")
        if spec.invalidated_by_mutation:
            if result.evaluated_revision != current_revision:
                failures.append("PASS result is stale for the current repository revision")
            if result.evaluated_fingerprint != current_fingerprint:
                failures.append("PASS result is stale for the current repository fingerprint")
            for item in referenced:
                if item.authority is not EvidenceAuthority.MACHINE:
                    continue
                if item.revision != current_revision or item.fingerprint != current_fingerprint:
                    failures.append(
                        f"authoritative evidence {item.evidence_id!r} is stale for the current revision"
                    )
        return _dedupe(failures)

    if result.status is FactoryGateStatus.NOT_APPLICABLE:
        if spec.applicable:
            failures.append("gate is declared applicable and cannot be marked NOT_APPLICABLE")
        if not (result.reason or "").strip():
            failures.append("NOT_APPLICABLE requires an explicit reason")
        if not result.evidence_ids:
            failures.append("NOT_APPLICABLE requires applicability evidence")
        if not any(
            item.authority in {EvidenceAuthority.MACHINE, EvidenceAuthority.USER_APPROVAL}
            for item in referenced
        ):
            failures.append("NOT_APPLICABLE requires authoritative applicability evidence")
        return _dedupe(failures)

    if not spec.applicable and result.status is not FactoryGateStatus.NOT_APPLICABLE:
        failures.append("resolved non-applicable gate must be recorded as NOT_APPLICABLE")

    return _dedupe(failures)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
