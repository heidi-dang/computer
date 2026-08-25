"""The single canonical FlowDeck agent registry."""

from __future__ import annotations

from cptr.flowdeck.contracts import AgentDefinition, AgentRole, Capability
from cptr.flowdeck.errors import RegistryError, UnknownAgentError

_READ_ONLY = frozenset(
    {
        Capability.READ_FILES,
        Capability.SEARCH_FILES,
        Capability.INSPECT_GIT,
        Capability.USE_BROWSER,
    }
)
_CODING = _READ_ONLY | frozenset({Capability.WRITE_FILES})

AGENT_REGISTRY: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        "heidi",
        AgentRole.PRIMARY,
        "FlowDeck coordinator for classification, strategy, delegation, and closure.",
        capabilities=_READ_ONLY,
        can_delegate=True,
        max_delegation_depth=1,
    ),
    AgentDefinition(
        "build-agent",
        AgentRole.SPECIALIST,
        "Executes one bounded implementation and verification loop under Heidi.",
        _CODING | frozenset({Capability.EXECUTE_COMMAND}),
    ),
    AgentDefinition(
        "planner", AgentRole.SPECIALIST,
        "Produces read-only implementation plans.", _READ_ONLY,
    ),
    AgentDefinition(
        "architect", AgentRole.SPECIALIST,
        "Reviews architecture and boundaries.", _READ_ONLY,
    ),
    AgentDefinition(
        "researcher", AgentRole.SPECIALIST,
        "Gathers and summarizes read-only evidence.", _READ_ONLY,
    ),
    AgentDefinition(
        "mapper", AgentRole.SPECIALIST,
        "Maps repositories and system boundaries.", _READ_ONLY,
    ),
    AgentDefinition(
        "reviewer", AgentRole.SPECIALIST,
        "Reviews evidence and implementation quality.", _READ_ONLY,
    ),
    AgentDefinition(
        "security-auditor",
        AgentRole.SPECIALIST,
        "Audits security boundaries without mutating the workspace.",
        _READ_ONLY,
    ),
    AgentDefinition(
        "backend-coder", AgentRole.SPECIALIST,
        "Applies controlled backend file changes.", _CODING,
    ),
    AgentDefinition(
        "frontend-coder", AgentRole.SPECIALIST,
        "Applies controlled frontend file changes.", _CODING,
    ),
    AgentDefinition(
        "browser-debugger", AgentRole.SPECIALIST,
        "Inspects the local preview without mutation.", _READ_ONLY,
    ),
    AgentDefinition(
        "tester", AgentRole.SPECIALIST,
        "Runs bounded structured repository checks.", frozenset({Capability.EXECUTE_COMMAND}),
    ),
    AgentDefinition(
        "debug-specialist",
        AgentRole.SPECIALIST,
        "Analyzes failures using read-only evidence.",
        _READ_ONLY,
    ),
    AgentDefinition(
        "designer",
        AgentRole.SPECIALIST,
        "Extracts and compares UI design evidence without changing the workspace.",
        _READ_ONLY | frozenset({Capability.DESIGN_INSPECTION}),
    ),
)


def validate_registry(registry: tuple[AgentDefinition, ...] = AGENT_REGISTRY) -> None:
    ids = [agent.id for agent in registry]
    if len(ids) != len(set(ids)):
        raise RegistryError("agent ids must be unique")
    if not any(agent.id == "heidi" and agent.role == AgentRole.PRIMARY for agent in registry):
        raise RegistryError("registry must contain primary agent heidi")
    for agent in registry:
        if not agent.id or not agent.id.strip():
            raise RegistryError("agent ids must be non-empty")
        if agent.role == AgentRole.SPECIALIST and agent.can_delegate:
            raise RegistryError(f"specialist {agent.id} cannot delegate")
        if agent.role == AgentRole.PRIMARY and agent.max_delegation_depth < 1:
            raise RegistryError(f"primary agent {agent.id} must allow one delegation level")
        if agent.max_delegation_depth < 0:
            raise RegistryError(f"agent {agent.id} has negative delegation depth")


def get_agent(
    agent_id: str,
    registry: tuple[AgentDefinition, ...] = AGENT_REGISTRY,
) -> AgentDefinition:
    for agent in registry:
        if agent.id == agent_id:
            return agent
    raise UnknownAgentError(f"unknown agent: {agent_id}")


validate_registry()