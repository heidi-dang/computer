"""FlowDeck delegation policy without task or agent execution."""

from __future__ import annotations

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import DelegationRequest
from cptr.flowdeck.errors import DelegationPolicyError
from cptr.flowdeck.registry import get_agent


def validate_delegation(request: DelegationRequest, config: FlowDeckConfig) -> None:
    parent = get_agent(request.parent_agent_id)
    child = get_agent(request.child_agent_id)
    if not parent.enabled:
        raise DelegationPolicyError(f"disabled parent agent: {parent.id}")
    if not child.enabled:
        raise DelegationPolicyError(f"disabled child agent: {child.id}")
    if parent.id == child.id:
        raise DelegationPolicyError("an agent cannot delegate to itself")
    if not parent.can_delegate:
        raise DelegationPolicyError(f"{parent.id} is not allowed to delegate")
    if child.role.value != "specialist":
        raise DelegationPolicyError(f"{parent.id} may only delegate to a specialist")
    if request.depth < 1 or request.depth > config.max_delegation_depth:
        raise DelegationPolicyError(
            f"delegation depth {request.depth} exceeds configured maximum "
            f"{config.max_delegation_depth}"
        )
    if request.requested_capabilities - child.capabilities:
        unknown = sorted(cap.value for cap in request.requested_capabilities - child.capabilities)
        raise DelegationPolicyError(f"child lacks requested capabilities: {', '.join(unknown)}")