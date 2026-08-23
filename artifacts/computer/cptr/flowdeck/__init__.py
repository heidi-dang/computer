"""FlowDeck-C-PTR coordination contracts and shadow routing.

FlowDeck owns policy and durable orchestration; CPTR remains the only
component that executes model/tool work.
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
from cptr.flowdeck.durable import (
    ApprovalStatus,
    AttemptStatus,
    DuplicateRequestError,
    DurableFlowDeck,
    LeaseUnavailableError,
    LifecycleError,
    OperationStatus,
    RecoveryGrant,
    RunStatus,
    StaleWriterError,
)
from cptr.flowdeck.execution import (
    MAPPER_CAPABILITIES,
    MAPPER_TOOL_NAMES,
    READ_ONLY_SPECIALIST_IDS,
    READ_ONLY_TOOL_NAMES,
    MapperPolicyError,
    MapperRequest,
    mapper_tool_guard,
    run_mapper,
    run_read_only_specialist,
    validate_mapper_request,
)
from cptr.flowdeck.gateway import observe_request
from cptr.flowdeck.registry import AGENT_REGISTRY, get_agent, validate_registry

__all__ = [
    "AGENT_REGISTRY",
    "MAPPER_CAPABILITIES",
    "MAPPER_TOOL_NAMES",
    "READ_ONLY_SPECIALIST_IDS",
    "READ_ONLY_TOOL_NAMES",
    "AgentDefinition",
    "AgentRole",
    "ApprovalStatus",
    "AttemptStatus",
    "Capability",
    "DelegationRequest",
    "DuplicateRequestError",
    "DurableFlowDeck",
    "FlowDeckConfig",
    "FlowDeckMode",
    "GovernanceDecision",
    "GovernanceVerdict",
    "LeaseUnavailableError",
    "LifecycleError",
    "MapperPolicyError",
    "MapperRequest",
    "OperationStatus",
    "RecoveryGrant",
    "RouteDecision",
    "RouteStrategy",
    "RunStatus",
    "ShadowDiagnostic",
    "StaleWriterError",
    "get_agent",
    "mapper_tool_guard",
    "observe_request",
    "run_mapper",
    "run_read_only_specialist",
    "validate_mapper_request",
    "validate_registry",
]