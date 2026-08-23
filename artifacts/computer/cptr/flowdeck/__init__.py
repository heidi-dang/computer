"""FlowDeck-C-PTR coordination contracts and shadow routing.

This package is intentionally execution-free in the first rollout. CPTR
remains the only component that executes model/tool work.
"""

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import (
    AgentDefinition,
    AgentRole,
    Capability,
    DelegationRequest,
    FlowDeckMode,
    GovernanceDecision,
    GovernanceVerdict,
    RouteDecision,
    RouteStrategy,
    ShadowDiagnostic,
)
from cptr.flowdeck.gateway import observe_request
from cptr.flowdeck.registry import AGENT_REGISTRY, get_agent, validate_registry

__all__ = [
    "AGENT_REGISTRY",
    "AgentDefinition",
    "AgentRole",
    "Capability",
    "DelegationRequest",
    "FlowDeckConfig",
    "FlowDeckMode",
    "GovernanceDecision",
    "GovernanceVerdict",
    "RouteDecision",
    "RouteStrategy",
    "ShadowDiagnostic",
    "get_agent",
    "observe_request",
    "validate_registry",
]