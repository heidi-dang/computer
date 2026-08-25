"""Bounded, repository-backed validation before Heidi dispatches any work."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
from pathlib import Path
import re
from typing import Any, Sequence

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.git import GitInspectionError, GitInspectionRequest, inspect_git
class PreExecutionValidationError(RuntimeError):
    """A validation failure that must stop execution before child dispatch."""


@dataclass(frozen=True)
class PreExecutionValidation:
    outcome: str
    reason: str
    facts: dict[str, Any]
    fingerprint: str


_FROZEN_CONFLICTS = (
    "mcp",
    "fdx",
    "unrestricted shell",
    "deploy",
    "publish",
    "git push",
    "dns",
    "credential rotation",
    "rotate credentials",
    "switch model",
    "switch provider",
    "automatic provider",
)
_UNAVAILABLE_CAPABILITIES = ("network access", "network call", "external api", "api key")
_MUTATION_TERMS = re.compile(
    r"\b(write|edit|modify|change|delete|remove|create|update|commit|push)\b",
    re.IGNORECASE,
)


def _workspace_facts(workspace: str) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        return {"workspace_exists": False, "git_available": False, "file_count": 0}
    count = 0
    for entry in root.iterdir():
        count += 1
        if count >= 200:
            break
    return {
        "workspace_exists": True,
        "git_available": (root / ".git").exists(),
        "file_count_bounded": count,
    }


async def _repository_facts(workspace: str, facts: dict[str, Any]) -> dict[str, Any]:
    if not facts["workspace_exists"] or not facts["git_available"]:
        return {"repository_state_verified": not facts["git_available"]}
    try:
        snapshot = await inspect_git(
            GitInspectionRequest(workspace=workspace, operation="status", limit=1),
            authorized_workspace=workspace,
        )
    except GitInspectionError:
        return {"repository_state_verified": False}
    status = "\n".join(snapshot.get("lines", []))
    return {
        "repository_state_verified": True,
        "git_status_digest": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


async def validate_pre_execution(
    *,
    task: str,
    workspace: str,
    model: str,
    plan: Sequence[Any],
    config: FlowDeckConfig,
    build_request: Any = None,
    audit_contract: dict[str, Any] | None = None,
) -> PreExecutionValidation:
    """Compare the request with live workspace facts and frozen policy bounds.

    This function is deterministic, bounded, and model-free. It returns only
    safe summaries; the raw task and connection data are never persisted.
    """
    await _yield_for_cancellation()
    text = (task or "").strip()
    lowered = text.casefold()
    facts = _workspace_facts(workspace)
    if inspect.isawaitable(facts):
        facts = await facts
    facts.update(await _repository_facts(workspace, facts))
    facts.update(
        {
            "model_present": bool(model.strip()),
            "plan_size": len(plan),
            "controlled_mode": config.mode.value == "controlled",
            "strict_governance": config.governance == "strict",
            "mutation_authorized": config.mutating_agents,
            "build_contract_present": build_request is not None,
            "audit_contract_present": audit_contract is not None,
        }
    )
    for phrase in _FROZEN_CONFLICTS:
        if phrase in lowered:
            return _result("rejected", f"the request conflicts with the frozen {phrase} boundary", facts)
    for phrase in _UNAVAILABLE_CAPABILITIES:
        if phrase in lowered:
            return _result("rejected", f"the requested {phrase} capability is unavailable", facts)
    if not text or len(text) < 3 or not plan:
        return _result("clarification", "the task is not specific enough to select a qualified path", facts)
    if not facts["workspace_exists"]:
        return _result("rejected", "the authorized workspace does not exist", facts)
    if not facts["repository_state_verified"]:
        return _result("rejected", "the repository state could not be verified", facts)
    if not model.strip():
        return _result("rejected", "no server-authorized CPTR model is available", facts)
    if not config.enabled or not config.coordinator_enabled:
        return _result("rejected", "controlled Heidi coordination is unavailable", facts)
    if config.mode.value != "controlled" or config.governance != "strict":
        return _result("rejected", "the request is incompatible with frozen strict governance", facts)
    if _MUTATION_TERMS.search(text) and not config.mutating_agents:
        return _result("rejected", "mutation is not currently authorized by FlowDeck policy", facts)
    for item in plan:
        if item.specialist_id in config.disabled_specialists:
            return _result("rejected", "a required specialist is disabled by server policy", facts)
    await _yield_for_cancellation()
    return _result("passed", "the task matches the live workspace and frozen execution boundaries", facts)


async def _yield_for_cancellation() -> None:
    import asyncio

    await asyncio.sleep(0)


def _result(outcome: str, reason: str, facts: dict[str, Any]) -> PreExecutionValidation:
    fingerprint = hashlib.sha256(
        f"{outcome}|{reason}|{sorted(facts.items())}".encode("utf-8")
    ).hexdigest()
    return PreExecutionValidation(outcome, reason, facts, fingerprint)