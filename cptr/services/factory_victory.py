"""Deterministic machine-authoritative Dark Factory Victory evaluation."""

from __future__ import annotations

from typing import Mapping, Sequence

from cptr.services.factory_gates import (
    FactoryGatePlan,
    FactoryGateStatus,
    GateEvidence,
    GateResult,
    validate_gate_evidence,
)

_VICTORY_ISSUER = object()


class FactoryVictoryDecision:
    """Opaque decision issued only by :class:`FactoryVictoryJudge`.

    ChatGPT/MCP/model payloads can serialize the public fields for inspection,
    but they cannot construct an instance without the private in-process issuer
    capability. CPTR's persistence layer additionally revalidates revision and
    fingerprint before authorizing the success transition.
    """

    __slots__ = (
        "passed",
        "failures",
        "satisfied_gate_ids",
        "evaluated_revision",
        "evaluated_fingerprint",
        "_issuer",
    )

    def __init__(
        self,
        *,
        passed: bool,
        failures: tuple[str, ...],
        satisfied_gate_ids: tuple[str, ...],
        evaluated_revision: str | None,
        evaluated_fingerprint: str | None,
        _issuer: object,
    ) -> None:
        if _issuer is not _VICTORY_ISSUER:
            raise TypeError("FactoryVictoryDecision can only be issued by FactoryVictoryJudge")
        self.passed = passed
        self.failures = failures
        self.satisfied_gate_ids = satisfied_gate_ids
        self.evaluated_revision = evaluated_revision
        self.evaluated_fingerprint = evaluated_fingerprint
        self._issuer = _issuer

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "failures": list(self.failures),
            "satisfied_gate_ids": list(self.satisfied_gate_ids),
            "evaluated_revision": self.evaluated_revision,
            "evaluated_fingerprint": self.evaluated_fingerprint,
        }


class FactoryVictoryJudge:
    """Evaluate a gate plan without consulting a model or worker self-report."""

    def evaluate(
        self,
        *,
        gate_plan: FactoryGatePlan,
        gate_results: Mapping[str, GateResult],
        evidence: Mapping[str, GateEvidence],
        current_revision: str | None,
        current_fingerprint: str | None,
        unresolved_security_findings: Sequence[str] = (),
    ) -> FactoryVictoryDecision:
        failures: list[str] = []
        satisfied: list[str] = []
        acceptance_covered: set[str] = set()

        for finding in unresolved_security_findings:
            finding = str(finding).strip()
            if finding:
                failures.append(f"unresolved blocking security/adversarial finding: {finding}")

        for spec in gate_plan.specs:
            result = gate_results.get(spec.gate_id)
            if result is None:
                if spec.required:
                    failures.append(f"required gate {spec.gate_id!r} is missing")
                continue

            validation_failures = validate_gate_evidence(
                spec,
                result,
                evidence,
                current_revision=current_revision,
                current_fingerprint=current_fingerprint,
            )

            if spec.required:
                if result.status is FactoryGateStatus.PENDING:
                    failures.append(f"required gate {spec.gate_id!r} is PENDING")
                elif result.status is FactoryGateStatus.FAIL:
                    failures.append(
                        f"required gate {spec.gate_id!r} failed: {result.reason or 'no reason recorded'}"
                    )
                elif result.status not in {
                    FactoryGateStatus.PASS,
                    FactoryGateStatus.NOT_APPLICABLE,
                }:
                    failures.append(
                        f"required gate {spec.gate_id!r} has unsupported status {result.status.value}"
                    )

                for reason in validation_failures:
                    failures.append(f"gate {spec.gate_id!r}: {reason}")

                if not validation_failures and result.status in {
                    FactoryGateStatus.PASS,
                    FactoryGateStatus.NOT_APPLICABLE,
                }:
                    satisfied.append(spec.gate_id)
                    if result.status is FactoryGateStatus.PASS:
                        acceptance_covered.update(spec.acceptance_ids)

        for criterion_id in gate_plan.acceptance_criterion_ids:
            if criterion_id not in acceptance_covered:
                failures.append(
                    f"acceptance criterion {criterion_id!r} is not covered by a valid PASS gate"
                )

        failures = list(dict.fromkeys(failures))
        return FactoryVictoryDecision(
            passed=not failures,
            failures=tuple(failures),
            satisfied_gate_ids=tuple(satisfied),
            evaluated_revision=current_revision,
            evaluated_fingerprint=current_fingerprint,
            _issuer=_VICTORY_ISSUER,
        )


def is_machine_issued_victory(decision: object) -> bool:
    """Return whether ``decision`` came from the server-owned Victory judge."""

    return isinstance(decision, FactoryVictoryDecision) and decision._issuer is _VICTORY_ISSUER
