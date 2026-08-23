"""Fail-closed capability governance for the shadow router."""

from __future__ import annotations

from collections.abc import Iterable

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import Capability, GovernanceDecision, GovernanceVerdict

HIGH_RISK_CAPABILITIES = frozenset(
    {
        Capability.EXECUTE_COMMAND,
        Capability.WRITE_FILES,
        Capability.MUTATE_GIT,
        Capability.USE_MCP,
        Capability.NETWORK_ACCESS,
    }
)


def evaluate_capabilities(
    requested: Iterable[Capability],
    *,
    granted: Iterable[Capability] = (),
    config: FlowDeckConfig,
) -> tuple[GovernanceDecision, ...]:
    granted_set = frozenset(granted)
    decisions = []
    for capability in sorted(set(requested), key=lambda item: item.value):
        if capability in HIGH_RISK_CAPABILITIES and capability not in granted_set:
            decisions.append(
                GovernanceDecision(
                    capability,
                    GovernanceVerdict.DENY,
                    "high-risk capability is not executable in this milestone",
                )
            )
        elif capability in granted_set:
            decisions.append(GovernanceDecision(capability, GovernanceVerdict.ALLOW, "explicitly granted"))
        elif config.governance == "strict":
            decisions.append(
                GovernanceDecision(capability, GovernanceVerdict.DENY, "strict governance lacks a grant")
            )
        else:
            decisions.append(
                GovernanceDecision(capability, GovernanceVerdict.UNKNOWN, "capability grant is unknown")
            )
    return tuple(decisions)


def strict_unknown_is_denied(decision: GovernanceDecision, config: FlowDeckConfig) -> GovernanceDecision:
    if config.governance == "strict" and decision.verdict == GovernanceVerdict.UNKNOWN:
        return GovernanceDecision(decision.capability, GovernanceVerdict.DENY, "strict mode denies UNKNOWN")
    return decision