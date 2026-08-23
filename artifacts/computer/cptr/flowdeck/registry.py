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

AGENT_REGISTRY: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        "heidi",
        AgentRole.PRIMARY,
        "FlowDeck coordinator for classification, strategy, delegation, and closure.",
        capabilities=_READ_ONLY,
        can_delegate=True,
        max_delegation_depth=1,
    ),
    AgentDefinition("planner", AgentRole.SPECIALIST, "Produces read-only implementation plans.", _READ_ONLY),
    AgentDefinition("architect", AgentRole.SPECIALIST, "Reviews architecture and boundaries.", _READ_ONLY),
    AgentDefinition("researcher", AgentRole.SPECIALIST, "Gathers and summarizes read-only evidence.", _READ_ONLY),
    AgentDefinition("mapper", AgentRole.SPECIALIST, "Maps repositories and system boundaries.", _READ_ONLY),
    AgentDefinition("reviewer", AgentRole.SPECIALIST, "Reviews evidence and implementation quality.", _READ_ONLY),
    AgentDefinition(
        "security-auditor",
        AgentRole.SPECIALIST,
        "Audits security boundaries without mutating the workspace.",
        _READ_ONLY,
    ),
    AgentDefinition(
        "debug-specialist",
        AgentRole.SPECIALIST,
        "Analyzes failures using read-only evidence.",
        _READ_ONLY,
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


def get_agent(agent_id: str, registry: tuple[AgentDefinition, ...] = AGENT_REGISTRY) -> AgentDefinition:
    for agent in registry:
        if agent.id == agent_id:
            return agent
    raise UnknownAgentError(f"unknown agent: {agent_id}")


validate_registry()