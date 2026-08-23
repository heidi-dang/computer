"""Independent verification contracts for autonomous supervisor decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


class IndependentVerifier(Protocol):
    async def verify(
        self, *, task: dict[str, Any], evidence: dict[str, Any], **kwargs: Any
    ) -> VerificationResult: ...


class DefaultIndependentVerifier:
    """Verify durable state and repository invariants independently of worker prose."""

    async def verify(
        self, *, task: dict[str, Any], evidence: dict[str, Any], **kwargs: Any
    ) -> VerificationResult:
        checks: list[dict[str, Any]] = []
        failures: list[str] = []
        terminal_success = str(task.get("status") or "").upper() in {
            "COMPLETE",
            "COMPLETED",
            "SUCCEEDED",
        }
        checks.append({"name": "durable_terminal_success", "passed": terminal_success})
        if not terminal_success:
            failures.append("worker did not reach a successful terminal state")

        independent = evidence.get("independent") or {}
        diff_check = independent.get("git_diff_check") or {}
        diff_passed = diff_check.get("passed", True)
        checks.append({"name": "git_diff_check", "passed": bool(diff_passed)})
        if not diff_passed:
            failures.append("git diff --check reported errors")

        return VerificationResult(passed=not failures, checks=checks, failures=failures)
