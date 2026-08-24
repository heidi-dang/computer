"""FlowDeck-C-PTR coordination contracts and shadow routing.

FlowDeck owns policy and durable orchestration; CPTR remains the only
component that executes model/tool work.
"""

from cptr.flowdeck.authenticated_gateway import (
    AuthenticatedGatewayError,
    SpecialistDispatchRequest,
    dispatch_authenticated_specialist,
    resolve_gateway_workspace,
)
from cptr.flowdeck.budgets import BudgetExceeded, RunBudget
from cptr.flowdeck.coding import (
    CODING_SPECIALIST_ROLES,
    CodingPolicyError,
    CodingRequest,
    browser_tool_guard,
    coding_tool_guard,
    coding_tool_names,
    validate_browser_request,
    validate_coding_request,
)
from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.build_agent import (
    BuildAgentPolicyError,
    BuildAgentRequest,
    run_build_agent,
    validate_build_agent_request,
)
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
from cptr.flowdeck.coordinator import (
    CoordinatorPolicyError,
    CoordinatorRequest,
    CoordinatorResult,
    PlannedDelegation,
    classify_coordinator_request,
    run_heidi_coordinator,
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
from cptr.flowdeck.evidence import EvidenceValidationError
from cptr.flowdeck.execution import (
    MAPPER_CAPABILITIES,
    MAPPER_TOOL_NAMES,
    READ_ONLY_SPECIALIST_IDS,
    READ_ONLY_TOOL_NAMES,
    MapperPolicyError,
    MapperRequest,
    mapper_tool_guard,
    validate_mapper_request,
)
from cptr.flowdeck.fdx import FDXConfig, FDXPolicyError, FDXResult, run_fdx, run_optional_fdx
from cptr.flowdeck.gateway import observe_request
from cptr.flowdeck.git_readonly import (
    GitInspectionPolicyError,
    GitInspectionRequest,
    inspect_git,
)
from cptr.flowdeck.registry import AGENT_REGISTRY, get_agent, validate_registry
from cptr.flowdeck.tester import (
    TEST_CHECKS,
    TesterPolicyError,
    TesterRequest,
    run_tester,
    validate_tester_request,
)

__all__ = [
    "AGENT_REGISTRY",
    "CODING_SPECIALIST_ROLES",
    "MAPPER_CAPABILITIES",
    "MAPPER_TOOL_NAMES",
    "READ_ONLY_SPECIALIST_IDS",
    "READ_ONLY_TOOL_NAMES",
    "TEST_CHECKS",
    "AgentDefinition",
    "AgentRole",
    "ApprovalStatus",
    "AttemptStatus",
    "BudgetExceeded",
    "BuildAgentPolicyError",
    "BuildAgentRequest",
    "AuthenticatedGatewayError",
    "Capability",
    "CodingPolicyError",
    "CodingRequest",
    "CoordinatorPolicyError",
    "CoordinatorRequest",
    "CoordinatorResult",
    "DelegationRequest",
    "DuplicateRequestError",
    "DurableFlowDeck",
    "EvidenceValidationError",
    "FDXConfig",
    "FDXPolicyError",
    "FDXResult",
    "FlowDeckConfig",
    "FlowDeckMode",
    "GitInspectionPolicyError",
    "GitInspectionRequest",
    "GovernanceDecision",
    "GovernanceVerdict",
    "LeaseUnavailableError",
    "LifecycleError",
    "MapperPolicyError",
    "MapperRequest",
    "OperationStatus",
    "PlannedDelegation",
    "RecoveryGrant",
    "RouteDecision",
    "RouteStrategy",
    "RunBudget",
    "RunStatus",
    "ShadowDiagnostic",
    "StaleWriterError",
    "TesterPolicyError",
    "TesterRequest",
    "browser_tool_guard",
    "coding_tool_guard",
    "coding_tool_names",
    "classify_coordinator_request",
    "get_agent",
    "dispatch_authenticated_specialist",
    "inspect_git",
    "mapper_tool_guard",
    "observe_request",
    "run_fdx",
    "run_build_agent",
    "run_heidi_coordinator",
    "run_optional_fdx",
    "run_tester",
    "resolve_gateway_workspace",
    "SpecialistDispatchRequest",
    "validate_browser_request",
    "validate_build_agent_request",
    "validate_coding_request",
    "validate_mapper_request",
    "validate_registry",
    "validate_tester_request",
]