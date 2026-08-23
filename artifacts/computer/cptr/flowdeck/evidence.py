"""Authoritative evidence contracts for FlowDeck terminal decisions."""

from __future__ import annotations

from typing import Any


TERMINAL_EVIDENCE_OUTCOMES = frozenset(
    {"succeeded", "failed", "cancelled", "unknown", "manual_review_required"}
)


class EvidenceValidationError(ValueError):
    """Raised when evidence cannot authoritatively decide a lifecycle gate."""


def bind_durable_identity(
    evidence: dict[str, Any],
    *,
    run_id: str,
    operation_id: str,
    step_id: str | None,
    workspace: str | None,
    owner: str,
    operation_fingerprint: str,
) -> dict[str, Any]:
    """Attach durable identity and reject caller-supplied identity mismatches."""
    binding = {
        "run_id": run_id,
        "operation_id": operation_id,
        "step_id": step_id,
        "workspace": workspace,
        "owner": owner,
        "operation_fingerprint": operation_fingerprint,
    }
    for key, expected in binding.items():
        if key in evidence and evidence[key] != expected:
            raise EvidenceValidationError(f"evidence {key} does not match durable identity")
    return {**evidence, **binding}


def validate_terminal_evidence(
    evidence: dict[str, Any] | None,
    *,
    outcome: str,
    attempt_id: str,
) -> dict[str, Any]:
    """Validate runtime/verifier evidence; specialist claims are never enough."""
    if outcome not in TERMINAL_EVIDENCE_OUTCOMES:
        raise EvidenceValidationError(f"unsupported terminal outcome: {outcome}")
    if not isinstance(evidence, dict):
        raise EvidenceValidationError("terminal evidence is required")
    if evidence.get("authoritative") is not True:
        raise EvidenceValidationError("terminal evidence must be authoritative")
    if evidence.get("source") not in {"runtime", "verifier"}:
        raise EvidenceValidationError("terminal evidence must come from runtime or verifier")
    if evidence.get("observed_outcome") != outcome:
        raise EvidenceValidationError("observed outcome does not match terminal outcome")
    if evidence.get("attempt_id") != attempt_id:
        raise EvidenceValidationError("evidence attempt identity does not match")
    if evidence.get("observation") not in {"native_loop_return", "verifier_check"}:
        raise EvidenceValidationError("evidence observation is not gate-deciding")
    if evidence.get("specialist_claim") is not None:
        raise EvidenceValidationError("specialist claims cannot be gate-deciding evidence")
    return evidence


def validate_reconciliation_evidence(
    evidence: dict[str, Any] | None,
    *,
    outcome: str,
) -> dict[str, Any]:
    """Validate evidence used to reconcile an interrupted operation."""
    if not isinstance(evidence, dict):
        raise EvidenceValidationError("reconciliation evidence is required")
    if evidence.get("authoritative") is not True:
        raise EvidenceValidationError("reconciliation evidence must be authoritative")
    if evidence.get("source") not in {"runtime", "verifier"}:
        raise EvidenceValidationError("reconciliation evidence must come from runtime or verifier")
    if evidence.get("observed_outcome") != outcome:
        raise EvidenceValidationError("reconciled outcome was not directly observed")
    if evidence.get("observation") != "verifier_check":
        raise EvidenceValidationError("reconciliation requires a verifier observation")
    return evidence