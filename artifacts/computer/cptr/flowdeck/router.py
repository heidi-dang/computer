"""Deterministic, side-effect-free FlowDeck classification and routing."""

from __future__ import annotations

import re

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import (
    Capability,
    FlowDeckMode,
    RouteDecision,
    RouteStrategy,
    ShadowDiagnostic,
)
from cptr.flowdeck.governance import evaluate_capabilities, strict_unknown_is_denied
from cptr.flowdeck.registry import get_agent

_SPECIALIST_HINTS: tuple[tuple[str, str], ...] = (
    ("security", "security-auditor"),
    ("vulnerability", "security-auditor"),
    ("architecture", "architect"),
    ("design", "architect"),
    ("research", "researcher"),
    ("investigate", "researcher"),
    ("map", "mapper"),
    ("repository", "mapper"),
    ("test", "reviewer"),
    ("review", "reviewer"),
    ("debug", "debug-specialist"),
    ("error", "debug-specialist"),
)
_MUTATION_TERMS = re.compile(
    r"\b(write|edit|modify|change|delete|remove|create|update|commit|push|run|execute)\b",
    re.IGNORECASE,
)
_READ_TERMS = re.compile(r"\b(read|inspect|list|search|find|research|review|map|analy[sz]e)\b", re.IGNORECASE)


def classify_request(content: str, model_id: str = "") -> RouteDecision:
    """Classify text without calling a model or inspecting the workspace."""
    text = (content or "").strip()
    lowered = text.casefold()
    specialists: list[str] = []
    for hint, specialist in _SPECIALIST_HINTS:
        if hint in lowered and specialist not in specialists:
            specialists.append(specialist)

    requested: set[Capability] = set()
    if _READ_TERMS.search(text):
        requested.update({Capability.READ_FILES, Capability.SEARCH_FILES})
    if "git" in lowered:
        requested.add(Capability.INSPECT_GIT)
    if "browser" in lowered or "website" in lowered or "url" in lowered:
        requested.add(Capability.USE_BROWSER)
    if _MUTATION_TERMS.search(text):
        requested.add(Capability.WRITE_FILES)
    if "command" in lowered or "shell" in lowered or "terminal" in lowered:
        requested.add(Capability.EXECUTE_COMMAND)

    if specialists:
        strategy = RouteStrategy.SPECIALIST
        rationale = "deterministic keyword hint selected a read-only specialist"
    elif model_id.startswith(("agent:", "claude_code:", "codex:", "cursor:")):
        strategy = RouteStrategy.EXTERNAL_AGENT
        rationale = "selected CPTR external-agent target; CPTR remains execution owner"
    else:
        strategy = RouteStrategy.DIRECT
        rationale = "no specialist or external-agent hint matched"

    return RouteDecision(
        strategy=strategy,
        specialist_ids=tuple(specialists),
        requested_capabilities=frozenset(requested),
        rationale=rationale,
    )


def shadow_route(content: str, model_id: str, config: FlowDeckConfig) -> ShadowDiagnostic:
    route = classify_request(content, model_id)
    warnings: list[str] = []
    for specialist_id in route.specialist_ids[: config.max_specialists]:
        get_agent(specialist_id)
    if len(route.specialist_ids) > config.max_specialists:
        warnings.append("specialist list was bounded by configuration")
    if config.mode != FlowDeckMode.SHADOW:
        warnings.append("mode is not shadow; no execution is provided by this milestone")
    governance = tuple(
        strict_unknown_is_denied(decision, config)
        for decision in evaluate_capabilities(route.requested_capabilities, config=config)
    )
    return ShadowDiagnostic(
        mode=config.mode,
        route=route,
        governance=governance,
        warnings=tuple(warnings),
        metadata={"execution": "native_cptr_only", "model_id_present": bool(model_id)},
    )