"""Deterministic FlowDeck domain contracts.

These types describe future orchestration; they do not execute tools, models,
commands, agents, or mutations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FlowDeckMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    READ_ONLY = "read_only"
    CONTROLLED = "controlled"
    FULL = "full"


class AgentRole(str, Enum):
    PRIMARY = "primary"
    SPECIALIST = "specialist"


class RouteStrategy(str, Enum):
    DIRECT = "direct"
    SPECIALIST = "specialist"
    EXTERNAL_AGENT = "external_agent"


class Capability(str, Enum):
    READ_FILES = "read_files"
    SEARCH_FILES = "search_files"
    INSPECT_GIT = "inspect_git"
    USE_BROWSER = "use_browser"
    EXECUTE_COMMAND = "execute_command"
    WRITE_FILES = "write_files"
    MUTATE_GIT = "mutate_git"
    USE_MCP = "use_mcp"
    NETWORK_ACCESS = "network_access"


class GovernanceVerdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AgentDefinition:
    id: str
    role: AgentRole
    description: str
    capabilities: frozenset[Capability] = frozenset()
    can_delegate: bool = False
    max_delegation_depth: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class DelegationRequest:
    parent_agent_id: str
    child_agent_id: str
    depth: int = 1
    requested_capabilities: frozenset[Capability] = frozenset()


@dataclass(frozen=True)
class GovernanceDecision:
    capability: Capability
    verdict: GovernanceVerdict
    reason: str


@dataclass(frozen=True)
class RouteDecision:
    strategy: RouteStrategy
    primary_agent_id: str = "heidi"
    specialist_ids: tuple[str, ...] = ()
    requested_capabilities: frozenset[Capability] = frozenset()
    rationale: str = ""


@dataclass(frozen=True)
class ShadowDiagnostic:
    mode: FlowDeckMode
    route: RouteDecision
    governance: tuple[GovernanceDecision, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)