"""FlowDeck-C-PTR coordination contracts and shadow routing.

FlowDeck owns policy and durable orchestration; CPTR remains the only
component that executes model/tool work.
"""

from cptr.flowdeck.budgets import BudgetExceeded, RunBudget
from cptr.flowdeck.coding import (
    CODING_SPECIALIST_ROLES,
    CodingPolicyError,
    CodingRequest,
    coding_tool_guard,
    coding_tool_names,
    run_coding_specialist,
    validate_coding_request,
)
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
from cptr.flowdeck.evidence import EvidenceValidationError
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
from cptr.flowdeck.fdx import FDXConfig, FDXPolicyError, FDXResult, run_fdx, run_optional_fdx
from cptr.flowdeck.gateway import observe_request
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
    "Capability",
    "CodingPolicyError",
    "CodingRequest",
    "DelegationRequest",
    "DuplicateRequestError",
    "DurableFlowDeck",
    "EvidenceValidationError",
    "FDXConfig",
    "FDXPolicyError",
    "FDXResult",
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
    "RunBudget",
    "RunStatus",
    "ShadowDiagnostic",
    "StaleWriterError",
    "TesterPolicyError",
    "TesterRequest",
    "coding_tool_guard",
    "coding_tool_names",
    "get_agent",
    "mapper_tool_guard",
    "observe_request",
    "run_coding_specialist",
    "run_fdx",
    "run_mapper",
    "run_optional_fdx",
    "run_read_only_specialist",
    "run_tester",
    "validate_coding_request",
    "validate_mapper_request",
    "validate_registry",
    "validate_tester_request",
]