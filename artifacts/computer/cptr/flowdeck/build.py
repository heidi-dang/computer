"""Bounded Heidi Build-mode contracts.

Build planning is deterministic in this phase. These records describe intent
and verification requirements; CPTR remains the only execution authority.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import Any


class BuildContractError(ValueError):
    """Raised when a Build request is not safe to accept."""


@dataclass(frozen=True)
class BuildBrief:
    version: int
    objective: str
    title: str
    user_flows: tuple[str, ...]
    required_screens: tuple[str, ...]
    data_model: tuple[str, ...]
    api_requirements: tuple[str, ...]
    authentication: str
    responsive_requirements: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BuildArchitecture:
    version: int
    frontend: str
    backend: str
    database: str
    authentication: str
    package_manager: str
    testing: str
    deployment: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BuildCompletionContract:
    version: int
    checks: tuple[str, ...]
    required_checks: tuple[str, ...]
    status: str = "PENDING"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BuildRequest:
    objective: str
    brief: BuildBrief
    architecture: BuildArchitecture
    completion: BuildCompletionContract

    def as_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "brief": self.brief.as_dict(),
            "architecture": self.architecture.as_dict(),
            "completion": self.completion.as_dict(),
        }


_BUILD_PREFIX = re.compile(r"^/build(?:\s+|$)", re.IGNORECASE)
_DEFAULT_CHECKS = (
    "startup",
    "primary_flow",
    "persistence",
    "responsive",
    "production_build",
    "runtime_health",
)


def parse_build_request(text: str) -> BuildRequest | None:
    """Parse explicit /build syntax without invoking a model or executor."""
    raw = (text or "").strip()
    if not _BUILD_PREFIX.match(raw):
        return None
    objective = _BUILD_PREFIX.sub("", raw, count=1).strip()
    if not objective:
        raise BuildContractError("Build needs an objective after /build")
    return create_build_request(objective)


def create_build_request(objective: str) -> BuildRequest:
    objective = " ".join((objective or "").split())
    if not objective or len(objective) > 20_000:
        raise BuildContractError("Build objective must contain 1-20000 characters")
    title = objective.rstrip(".!?")[:80].strip() or "New application"
    brief = BuildBrief(
        version=1,
        objective=objective,
        title=title,
        user_flows=("Complete the requested primary flow.",),
        required_screens=("Primary screen for the requested flow.",),
        data_model=("Infer only from the requested flow; confirm before destructive changes.",),
        api_requirements=("Infer only endpoints required by the requested flow.",),
        authentication="Reuse an existing project provider when present; otherwise ask only when required.",
        responsive_requirements=("Support narrow mobile and desktop layouts.",),
        acceptance_criteria=(
            "The application starts successfully.",
            "The requested primary flow is usable.",
            "Required data persists across reload.",
            "The primary flow works at mobile and desktop widths.",
            "The production build succeeds.",
            "Runtime health and browser errors are reviewed before completion.",
        ),
    )
    architecture = BuildArchitecture(
        version=1,
        frontend=os.getenv("CPTR_BUILD_FRONTEND", "detect-existing"),
        backend=os.getenv("CPTR_BUILD_BACKEND", "detect-existing"),
        database=os.getenv("CPTR_BUILD_DATABASE", "detect-existing"),
        authentication=os.getenv("CPTR_BUILD_AUTHENTICATION", "reuse-existing"),
        package_manager=os.getenv("CPTR_BUILD_PACKAGE_MANAGER", "detect-existing"),
        testing=os.getenv("CPTR_BUILD_TESTING", "detect-existing"),
        deployment=os.getenv("CPTR_BUILD_DEPLOYMENT", "local-until-approved"),
    )
    completion = BuildCompletionContract(
        version=1,
        checks=_DEFAULT_CHECKS,
        required_checks=_DEFAULT_CHECKS,
    )
    return BuildRequest(objective, brief, architecture, completion)


def build_contract_is_satisfied(
    contract: BuildCompletionContract,
    evidence: dict[str, str] | None,
) -> bool:
    """Return true only when every required check has a verified pass."""
    evidence = evidence or {}
    return all(evidence.get(check) == "VERIFIED" for check in contract.required_checks)


def build_initial_message(request: BuildRequest) -> str:
    return (
        f"Build mode · preparing a brief for “{request.brief.title}”. "
        "I’ll inspect the workspace, choose safe defaults, and verify each "
        "app requirement before claiming completion."
    )