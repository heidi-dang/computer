"""Typed Capability DAG compiler with fail-closed effect and rollback checks."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from cptr.services.capability_os.contracts import CapabilityRequest


class CapabilityCompileError(ValueError):
    pass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    only_if_idempotent: bool = True
    retryable_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")


@dataclass(frozen=True)
class DagNode:
    id: str
    action_ref: str
    version: str
    input_bindings: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    permissions: tuple[CapabilityRequest, ...] = ()
    timeout_ms: int = 30_000
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    compensation_action_ref: str | None = None
    approval: str = "none"
    idempotent: bool = False

    def __post_init__(self) -> None:
        for name in ("id", "action_ref", "version"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")
        if self.timeout_ms <= 0:
            raise ValueError("node timeout must be positive")
        if self.approval not in {"none", "policy", "human"}:
            raise ValueError("invalid node approval mode")


@dataclass(frozen=True)
class DagEdge:
    source: str
    target: str


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    version: str
    inputs_schema: dict[str, Any]
    preconditions: tuple[Any, ...]
    effects: tuple[CapabilityRequest, ...]
    nodes: tuple[DagNode, ...]
    edges: tuple[DagEdge, ...]
    verifiers: tuple[str, ...]
    rollback_mode: str
    permissions: tuple[CapabilityRequest, ...]
    deadline_ms: int
    max_parallelism: int
    risk_class: str

    def __post_init__(self) -> None:
        if not self.capability_id.strip() or not self.version.strip():
            raise ValueError("capability id and version must not be blank")
        if self.rollback_mode not in {"full", "compensating", "partial", "irreversible"}:
            raise ValueError("invalid rollback mode")
        if self.risk_class not in {"read", "reversible-write", "high-impact", "critical"}:
            raise ValueError("invalid risk class")
        if self.deadline_ms <= 0 or self.max_parallelism <= 0:
            raise ValueError("capability execution bounds must be positive")


@dataclass(frozen=True)
class CompiledCapability:
    spec: CapabilitySpec
    topological_order: tuple[str, ...]
    permissions: tuple[CapabilityRequest, ...]
    requires_human_approval: bool


def _covered(requested: CapabilityRequest, declared: CapabilityRequest) -> bool:
    return requested.action == declared.action and (
        requested.resource == declared.resource
        or fnmatch.fnmatchcase(requested.resource, declared.resource)
    )


def _write_like(permission: CapabilityRequest) -> bool:
    action = permission.action
    return any(
        marker in action
        for marker in ("write", "delete", "deploy", "update", "create", "restart", "privileged")
    )


class CapabilityCompiler:
    def compile(self, spec: CapabilitySpec) -> CompiledCapability:
        nodes: dict[str, DagNode] = {}
        for node in spec.nodes:
            if node.id in nodes:
                raise CapabilityCompileError(f"duplicate node id: {node.id}")
            nodes[node.id] = node
        if not nodes:
            raise CapabilityCompileError("capability recipe must contain at least one node")

        declared = tuple(spec.permissions)
        for effect in spec.effects:
            if not any(_covered(effect, permission) for permission in declared):
                raise CapabilityCompileError("declared effect is not covered by capability permission")
        for node in nodes.values():
            for permission in node.permissions:
                if not any(_covered(permission, item) for item in declared):
                    raise CapabilityCompileError(
                        f"node {node.id} permission exceeds capability permission envelope"
                    )
            if node.retry.max_attempts > 1 and node.retry.only_if_idempotent and not node.idempotent:
                raise CapabilityCompileError(
                    f"node {node.id} retry requires an idempotent action declaration"
                )

        incoming = {node_id: 0 for node_id in nodes}
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        for edge in spec.edges:
            if edge.source not in nodes or edge.target not in nodes:
                raise CapabilityCompileError("DAG edge references unknown node")
            outgoing[edge.source].append(edge.target)
            incoming[edge.target] += 1

        ready = sorted(node_id for node_id, count in incoming.items() if count == 0)
        order: list[str] = []
        while ready:
            node_id = ready.pop(0)
            order.append(node_id)
            for target in sorted(outgoing[node_id]):
                incoming[target] -= 1
                if incoming[target] == 0:
                    ready.append(target)
                    ready.sort()
        if len(order) != len(nodes):
            raise CapabilityCompileError("capability DAG contains a cycle")

        unknown_verifiers = sorted(set(spec.verifiers) - set(nodes))
        if unknown_verifiers:
            raise CapabilityCompileError("verifier references unknown node")

        if spec.risk_class in {"high-impact", "critical"}:
            if spec.rollback_mode == "irreversible":
                requires_human = True
            else:
                write_nodes = [
                    node for node in nodes.values() if any(_write_like(item) for item in node.permissions)
                ]
                if spec.rollback_mode in {"compensating", "partial"} and any(
                    not node.compensation_action_ref for node in write_nodes
                ):
                    raise CapabilityCompileError(
                        "high-impact capability rollback is missing compensation coverage"
                    )
                requires_human = True
        else:
            requires_human = any(node.approval == "human" for node in nodes.values())

        if any(node.approval == "human" for node in nodes.values()):
            requires_human = True

        return CompiledCapability(
            spec=spec,
            topological_order=tuple(order),
            permissions=tuple(sorted(declared, key=lambda item: (item.action, item.resource))),
            requires_human_approval=requires_human,
        )
