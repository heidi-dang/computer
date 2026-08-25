"""Bounded adaptive routing policy for Phase 11.

This module only chooses an existing CPTR/FlowDeck path. It never calls a
model, changes the selected model, executes tools, or creates a second loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from cptr.flowdeck.router import classify_request


@dataclass(frozen=True)
class AdaptiveRoute:
    path: str
    rationale: str
    specialist_ids: tuple[str, ...]


def adaptive_route(content: str, model_id: str = "") -> AdaptiveRoute:
    decision = classify_request(content, model_id)
    if decision.strategy.value == "direct" and not decision.requested_capabilities:
        return AdaptiveRoute(
            "native_direct",
            "simple request uses the native CPTR path",
            (),
        )
    return AdaptiveRoute(
        "flowdeck_specialist",
        "capabilities or specialist hints require the existing FlowDeck DAG path",
        decision.specialist_ids,
    )