"""Fail-closed analysis of durable repository facts.

Analysis is deliberately separate from specialist prose. It can only promote
facts that contain in-workspace evidence; missing or tampered evidence remains
UNKNOWN/UNVERIFIED and never becomes PASS.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

ANALYSIS_CONTRACT_VERSION = "audit-analysis-v1"
CHECK_STATUSES = frozenset({"passed", "unverified", "failed", "manual_review_required"})
SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})
CONFIDENCES = frozenset({"high", "medium", "low"})
RISK_CATEGORIES = (
    "correctness",
    "root_cause_risk",
    "security",
    "concurrency",
    "cancellation_recovery",
    "integrity",
    "migrations",
    "errors",
    "performance",
    "quality",
    "test_gaps",
    "dead_logic",
    "drift",
    "ui_api_contracts",
    "provider_fail_closed",
    "readiness",
)
_CATEGORY_TO_FACT = {
    "correctness": "architecture",
    "root_cause_risk": "architecture",
    "security": "authentication_authorization",
    "concurrency": "state_ownership",
    "cancellation_recovery": "state_ownership",
    "integrity": "data_flows",
    "migrations": "migrations",
    "errors": "runtime_entry_points",
    "performance": "runtime_entry_points",
    "quality": "module_boundaries",
    "test_gaps": "tests",
    "dead_logic": "module_boundaries",
    "drift": "recent_changes",
    "ui_api_contracts": "data_flows",
    "provider_fail_closed": "provider_boundaries",
    "readiness": "runtime_entry_points",
}


class AuditAnalysisError(ValueError):
    """Raised when an analysis payload cannot support an authoritative result."""


@dataclass(frozen=True)
class AuditCheck:
    category: str
    status: str
    summary: str
    evidence: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "status": self.status,
            "summary": self.summary,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class AuditFinding:
    finding_id: str
    category: str
    severity: str
    confidence: str
    title: str
    evidence: tuple[str, ...]
    impact: str
    scope: str
    remediation: str
    status: str = "unverified"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.finding_id,
            "category": self.category,
            "severity": self.severity,
            "confidence": self.confidence,
            "title": self.title,
            "evidence": list(self.evidence),
            "impact": self.impact,
            "scope": self.scope,
            "remediation": self.remediation,
            "status": self.status,
        }


@dataclass(frozen=True)
class AuditAnalysis:
    contract_version: str
    checks: tuple[AuditCheck, ...]
    findings: tuple[AuditFinding, ...]
    overall_status: str
    unknown_is_not_pass: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "checks": [check.as_dict() for check in self.checks],
            "findings": [finding.as_dict() for finding in self.findings],
            "overall_status": self.overall_status,
            "unknown_is_not_pass": self.unknown_is_not_pass,
        }


def _finding_id(category: str, title: str, evidence: tuple[str, ...]) -> str:
    raw = json.dumps([category, title, evidence], separators=(",", ":"), sort_keys=True)
    return "AF-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _validate_fact(fact: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    category = str(fact.get("category") or "")
    status = str(fact.get("status") or "").lower()
    evidence_raw = fact.get("evidence")
    if not category or status not in {"verified", "unknown"} or not isinstance(evidence_raw, list):
        raise AuditAnalysisError("repository fact has an invalid schema")
    evidence = tuple(str(item) for item in evidence_raw if isinstance(item, str) and item)
    if any(path.startswith("/") or ".." in path.split("/") for path in evidence):
        raise AuditAnalysisError("repository fact evidence escapes the workspace")
    if status == "verified" and not evidence:
        raise AuditAnalysisError("verified repository facts require evidence")
    return category, status, evidence


def _finding_for_unverified(
    category: str,
    fact_category: str,
    evidence: tuple[str, ...],
    *,
    fact_status: str,
) -> AuditFinding:
    evidence = evidence or (f"fact:{fact_category}:unknown",)
    title = (
        f"{category.replace('_', ' ').title()} requires independent risk verification"
        if fact_status == "verified"
        else f"{category.replace('_', ' ').title()} cannot be verified from bounded evidence"
    )
    return AuditFinding(
        finding_id=_finding_id(category, title, evidence),
        category=category,
        severity="info" if fact_status == "verified" else "low",
        confidence="high",
        title=title,
        evidence=evidence,
        impact=(
            "Repository paths are present, but path inventory alone cannot establish "
            "the absence of a production risk."
            if fact_status == "verified"
            else "The audit cannot make a reliable production conclusion for this risk area."
        ),
        scope=f"Repository evidence category: {fact_category}",
        remediation=(
            "Run the qualified read-only semantic inspection for this category and "
            "retain runtime/verifier evidence before marking the check passed."
        ),
    )


def validate_analysis(analysis: AuditAnalysis) -> AuditAnalysis:
    if analysis.contract_version != ANALYSIS_CONTRACT_VERSION:
        raise AuditAnalysisError("unsupported analysis contract version")
    if not analysis.unknown_is_not_pass:
        raise AuditAnalysisError("analysis must preserve UNKNOWN as not PASS")
    seen_categories: set[str] = set()
    seen_ids: set[str] = set()
    for check in analysis.checks:
        if check.category not in RISK_CATEGORIES or check.category in seen_categories:
            raise AuditAnalysisError("analysis checks must cover each risk category once")
        if check.status not in CHECK_STATUSES:
            raise AuditAnalysisError("analysis check has an unsupported status")
        if check.status == "passed" and not check.evidence:
            raise AuditAnalysisError("PASS checks require evidence")
        seen_categories.add(check.category)
    if seen_categories != set(RISK_CATEGORIES):
        raise AuditAnalysisError("analysis must cover all required risk categories")
    for finding in analysis.findings:
        if finding.finding_id in seen_ids or finding.category not in RISK_CATEGORIES:
            raise AuditAnalysisError("analysis findings must be unique and categorized")
        if finding.severity not in SEVERITIES or finding.confidence not in CONFIDENCES:
            raise AuditAnalysisError("finding severity or confidence is unsupported")
        if not finding.evidence or not finding.impact or not finding.scope or not finding.remediation:
            raise AuditAnalysisError("findings require evidence, impact, scope, and remediation")
        if finding.status == "passed":
            raise AuditAnalysisError("findings cannot claim PASS")
        seen_ids.add(finding.finding_id)
    return analysis


def analyze_repository_facts(inspection: dict[str, Any]) -> AuditAnalysis:
    """Convert only validated repository facts into deterministic checks/findings."""
    if inspection.get("read_only") is not True:
        raise AuditAnalysisError("inspection is not explicitly read-only")
    facts_raw = inspection.get("facts")
    if not isinstance(facts_raw, list):
        raise AuditAnalysisError("inspection facts are required")
    by_category: dict[str, tuple[str, tuple[str, ...]]] = {}
    for raw_fact in facts_raw:
        if not isinstance(raw_fact, dict):
            raise AuditAnalysisError("inspection fact must be an object")
        category, status, evidence = _validate_fact(raw_fact)
        if category in by_category:
            raise AuditAnalysisError("duplicate repository fact category")
        by_category[category] = (status, evidence)

    checks: list[AuditCheck] = []
    findings: dict[str, AuditFinding] = {}
    for category in RISK_CATEGORIES:
        fact_category = _CATEGORY_TO_FACT[category]
        status, evidence = by_category.get(fact_category, ("unknown", ()))
        check = AuditCheck(
            category=category,
            status="unverified",
            summary=(
                f"Evidence paths are available for {category}, but semantic risk "
                "verification is not established."
                if status == "verified"
                else f"UNKNOWN: bounded evidence is unavailable for {category}."
            ),
            evidence=evidence,
        )
        finding = _finding_for_unverified(
            category,
            fact_category,
            evidence,
            fact_status=status,
        )
        findings[finding.finding_id] = finding
        checks.append(check)

    analysis = AuditAnalysis(
        contract_version=ANALYSIS_CONTRACT_VERSION,
        checks=tuple(checks),
        findings=tuple(sorted(findings.values(), key=lambda finding: finding.finding_id)),
        overall_status="unverified",
    )
    return validate_analysis(analysis)
