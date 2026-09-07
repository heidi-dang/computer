"""Immutable Skill Genome synthesis and matched-budget evaluation.

Skills are strategic artefacts only. This module intentionally contains no
permission, credential, network, or runtime-authority fields.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Any

from cptr.services.capability_os.authority import AuthorityBroker
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    CapabilityRequest,
    CptrArtifact,
    create_artifact,
)
from cptr.services.capability_os.store import SqlCapabilityOsStore


_SKILL_OPERATORS = {
    "fork",
    "mutate",
    "recombine",
    "simplify",
    "generalise",
    "specialise",
}


@dataclass(frozen=True)
class SkillMcpStep:
    id: str
    tool_name: str
    depends_on: tuple[str, ...] = ()
    input_bindings: dict[str, Any] = field(default_factory=dict)
    timeout_ms: int = 30_000
    verifier: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.tool_name.strip():
            raise ValueError("skill MCP step id/tool must not be blank")
        if self.timeout_ms <= 0:
            raise ValueError("skill MCP step timeout must be positive")
        if self.id in self.depends_on:
            raise ValueError("skill MCP step cannot depend on itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("skill MCP step dependencies must be unique")
        if not isinstance(self.input_bindings, dict):
            raise TypeError("skill MCP step input bindings must be an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool_name,
            "dependsOn": list(self.depends_on),
            "inputBindings": dict(self.input_bindings),
            "timeoutMs": self.timeout_ms,
            "verifier": self.verifier,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillMcpStep":
        if not isinstance(value, dict):
            raise TypeError("skill MCP step must be an object")
        return cls(
            id=str(value.get("id") or ""),
            tool_name=str(value.get("tool") or ""),
            depends_on=tuple(str(item) for item in value.get("dependsOn") or ()),
            input_bindings=dict(value.get("inputBindings") or {}),
            timeout_ms=int(value.get("timeoutMs") or 30_000),
            verifier=bool(value.get("verifier", False)),
        )


@dataclass(frozen=True)
class SkillGenome:
    objective: str
    assumptions: tuple[str, ...]
    decomposition_strategy: tuple[str, ...]
    decision_rules: tuple[str, ...]
    evidence_policy: tuple[str, ...]
    tool_selection_heuristics: tuple[str, ...]
    stopping_conditions: tuple[str, ...]
    failure_recovery: tuple[str, ...]
    verification_requirements: tuple[str, ...]
    output_contract: str
    activation_domains: tuple[str, ...]
    activation_task_patterns: tuple[str, ...]
    activation_exclusions: tuple[str, ...]
    context_resources: tuple[str, ...]
    max_injected_tokens: int | None
    benchmark_suite: str | None
    primary_metric: str
    guardrail_metrics: tuple[str, ...]
    mcp_steps: tuple[SkillMcpStep, ...] = ()

    def __post_init__(self) -> None:
        if not self.objective.strip() or not self.output_contract.strip():
            raise ValueError("skill objective and output contract must not be blank")
        if not self.primary_metric.strip():
            raise ValueError("skill primary metric must not be blank")
        if not self.activation_domains or not self.activation_task_patterns:
            raise ValueError("skill activation must declare domain and task pattern")
        if self.max_injected_tokens is not None and self.max_injected_tokens <= 0:
            raise ValueError("skill max_injected_tokens must be positive")
        step_ids = [step.id for step in self.mcp_steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("skill MCP step ids must be unique")
        known_steps = set(step_ids)
        for step in self.mcp_steps:
            unknown = set(step.depends_on) - known_steps
            if unknown:
                raise ValueError(f"skill MCP step depends on unknown step: {sorted(unknown)[0]}")
        for name in (
            "assumptions",
            "decomposition_strategy",
            "decision_rules",
            "evidence_policy",
            "tool_selection_heuristics",
            "stopping_conditions",
            "failure_recovery",
            "verification_requirements",
            "activation_domains",
            "activation_task_patterns",
            "context_resources",
            "guardrail_metrics",
        ):
            if any(not str(item).strip() for item in getattr(self, name)):
                raise ValueError(f"skill {name} contains a blank item")

    def to_spec(
        self,
        *,
        parent: str | None = None,
        operator: str | None = None,
    ) -> dict[str, Any]:
        lineage: dict[str, Any] = {}
        if parent is not None:
            lineage["parent"] = parent
        if operator is not None:
            lineage["operator"] = operator
        return {
            "objective": self.objective,
            "assumptions": list(self.assumptions),
            "decompositionStrategy": list(self.decomposition_strategy),
            "decisionRules": list(self.decision_rules),
            "evidencePolicy": list(self.evidence_policy),
            "toolSelectionHeuristics": list(self.tool_selection_heuristics),
            "stoppingConditions": list(self.stopping_conditions),
            "failureRecovery": list(self.failure_recovery),
            "verificationRequirements": list(self.verification_requirements),
            "outputContract": self.output_contract,
            "activation": {
                "domains": list(self.activation_domains),
                "taskPatterns": list(self.activation_task_patterns),
                "exclusions": list(self.activation_exclusions),
            },
            "context": {
                "resources": list(self.context_resources),
                "maxInjectedTokens": self.max_injected_tokens,
            },
            "evaluation": {
                "benchmarkSuite": self.benchmark_suite,
                "primaryMetric": self.primary_metric,
                "guardrailMetrics": list(self.guardrail_metrics),
            },
            "executionHints": {
                "mcpSteps": [step.to_dict() for step in self.mcp_steps],
            },
            "lineage": lineage,
        }

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "SkillGenome":
        activation = dict(spec.get("activation") or {})
        context = dict(spec.get("context") or {})
        evaluation = dict(spec.get("evaluation") or {})
        execution_hints = dict(spec.get("executionHints") or {})
        return cls(
            objective=str(spec.get("objective") or ""),
            assumptions=tuple(str(item) for item in spec.get("assumptions") or []),
            decomposition_strategy=tuple(
                str(item) for item in spec.get("decompositionStrategy") or []
            ),
            decision_rules=tuple(str(item) for item in spec.get("decisionRules") or []),
            evidence_policy=tuple(str(item) for item in spec.get("evidencePolicy") or []),
            tool_selection_heuristics=tuple(
                str(item) for item in spec.get("toolSelectionHeuristics") or []
            ),
            stopping_conditions=tuple(
                str(item) for item in spec.get("stoppingConditions") or []
            ),
            failure_recovery=tuple(str(item) for item in spec.get("failureRecovery") or []),
            verification_requirements=tuple(
                str(item) for item in spec.get("verificationRequirements") or []
            ),
            output_contract=str(spec.get("outputContract") or ""),
            activation_domains=tuple(str(item) for item in activation.get("domains") or []),
            activation_task_patterns=tuple(
                str(item) for item in activation.get("taskPatterns") or []
            ),
            activation_exclusions=tuple(
                str(item) for item in activation.get("exclusions") or []
            ),
            context_resources=tuple(str(item) for item in context.get("resources") or []),
            max_injected_tokens=(
                int(context["maxInjectedTokens"])
                if context.get("maxInjectedTokens") is not None
                else None
            ),
            benchmark_suite=(
                str(evaluation["benchmarkSuite"])
                if evaluation.get("benchmarkSuite") is not None
                else None
            ),
            primary_metric=str(evaluation.get("primaryMetric") or ""),
            guardrail_metrics=tuple(
                str(item) for item in evaluation.get("guardrailMetrics") or []
            ),
            mcp_steps=tuple(
                SkillMcpStep.from_dict(item)
                for item in execution_hints.get("mcpSteps") or []
            ),
        )


@dataclass(frozen=True)
class SkillEvaluationArm:
    runs: int
    successes: int
    regressions: int
    policy_violations: int
    model_id: str
    reasoning_effort: str
    tool_permission_fingerprint: str
    resource_budget_fingerprint: str
    task_distribution_fingerprint: str
    mean_tokens: float
    mean_tool_calls: float

    def __post_init__(self) -> None:
        if min(self.runs, self.successes, self.regressions, self.policy_violations) < 0:
            raise ValueError("skill evaluation counts must be non-negative")
        if self.successes > self.runs or self.regressions > self.runs:
            raise ValueError("skill evaluation outcomes cannot exceed runs")
        if self.mean_tokens < 0 or self.mean_tool_calls < 0:
            raise ValueError("skill evaluation costs must be non-negative")

    @property
    def success_rate(self) -> float:
        return self.successes / self.runs if self.runs else 0.0

    @property
    def regression_rate(self) -> float:
        return self.regressions / self.runs if self.runs else 0.0


@dataclass(frozen=True)
class SkillEvaluationResult:
    promotable: bool
    reason: str
    success_delta: float
    token_delta: float
    tool_call_delta: float


class SkillEvaluationError(ValueError):
    pass


class SkillEvaluator:
    def __init__(self, *, min_runs_per_arm: int = 5, max_regression_rate: float = 0.02) -> None:
        if min_runs_per_arm <= 1:
            raise ValueError("skill evaluation requires repeated runs")
        if not 0 <= max_regression_rate <= 1:
            raise ValueError("max_regression_rate must be within [0, 1]")
        self.min_runs_per_arm = int(min_runs_per_arm)
        self.max_regression_rate = float(max_regression_rate)

    def compare(
        self,
        *,
        baseline: SkillEvaluationArm,
        candidate: SkillEvaluationArm,
    ) -> SkillEvaluationResult:
        matched_fields = (
            "model_id",
            "reasoning_effort",
            "tool_permission_fingerprint",
            "resource_budget_fingerprint",
            "task_distribution_fingerprint",
        )
        for name in matched_fields:
            if getattr(baseline, name) != getattr(candidate, name):
                raise SkillEvaluationError(f"skill experiment is not matched on {name}")

        success_delta = candidate.success_rate - baseline.success_rate
        token_delta = candidate.mean_tokens - baseline.mean_tokens
        tool_call_delta = candidate.mean_tool_calls - baseline.mean_tool_calls
        if baseline.runs < self.min_runs_per_arm or candidate.runs < self.min_runs_per_arm:
            return SkillEvaluationResult(
                promotable=False,
                reason="insufficient-evidence",
                success_delta=success_delta,
                token_delta=token_delta,
                tool_call_delta=tool_call_delta,
            )
        if candidate.policy_violations > baseline.policy_violations:
            reason = "policy-regression"
        elif candidate.regression_rate > self.max_regression_rate:
            reason = "regression-threshold"
        elif candidate.regression_rate > baseline.regression_rate:
            reason = "regression-worse-than-baseline"
        elif success_delta <= 0:
            reason = "no-success-improvement"
        else:
            reason = "matched-evidence-improved"
        return SkillEvaluationResult(
            promotable=reason == "matched-evidence-improved",
            reason=reason,
            success_delta=success_delta,
            token_delta=token_delta,
            tool_call_delta=tool_call_delta,
        )


@dataclass(frozen=True)
class SkillMcpActivation:
    capability: CptrArtifact[Any]
    skill_digest: str
    mount_ids: tuple[str, ...]
    projected_actions: tuple[str, ...]


class SkillMcpActivator:
    """Bind an authority-free Skill Genome to already-mounted MCP projections.

    The skill stores only semantic tool names. Mount IDs, server identities,
    permissions, and remote authority are derived from current server-owned
    state at activation time and remain task-scoped.
    """

    _REUSABLE_STATES = frozenset({
        ArtifactState.QUALIFIED.value,
        ArtifactState.LEARNED.value,
        ArtifactState.CERTIFIED.value,
        ArtifactState.CORE.value,
    })

    def __init__(self, *, store: SqlCapabilityOsStore, authority: AuthorityBroker, clock_ms) -> None:
        self._store = store
        self._authority = authority
        self._clock_ms = clock_ms

    def _created_at(self) -> str:
        seconds = int(self._clock_ms()) / 1000
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

    async def activate(
        self,
        *,
        skill_digest: str,
        user_id: str,
        task_id: str,
    ) -> SkillMcpActivation:
        row = await self._store.get_artifact(skill_digest, user_id=user_id, include_global=True)
        if row is None or row.kind != ArtifactKind.SKILL.value:
            raise KeyError("skill artifact not found")
        if row.state not in self._REUSABLE_STATES:
            raise PermissionError("skill must be qualified before MCP activation")
        genome = SkillGenome.from_spec(dict(row.spec or {}))
        if not genome.mcp_steps:
            raise ValueError("skill has no MCP execution hints")

        mounts = await self._store.list_active_mounts(task_id)
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        permissions: dict[tuple[str, str], CapabilityRequest] = {}
        mount_ids: list[str] = []
        projected_actions: list[str] = []

        for step in genome.mcp_steps:
            candidates = [
                mount for mount in mounts
                if step.tool_name in tuple(mount.projected_tools or ())
            ]
            if len(candidates) != 1:
                raise PermissionError(
                    f"skill MCP tool requires one active projected mount: {step.tool_name}"
                )
            mount = candidates[0]
            adapter = await self._store.get_artifact(
                mount.digest, user_id=user_id, include_global=True
            )
            if (
                adapter is None
                or adapter.kind != ArtifactKind.MCP_ADAPTER.value
                or adapter.state not in self._REUSABLE_STATES
            ):
                raise PermissionError("skill MCP adapter is unavailable or unqualified")
            adapter_spec = dict(adapter.spec or {})
            server_id = str(adapter_spec.get("serverId") or "").strip()
            if not server_id:
                raise PermissionError("skill MCP adapter lacks server-owned identity")
            permission = CapabilityRequest(
                "mcp.invoke", f"mcp:{server_id}/{step.tool_name}"
            )
            await self._authority.require_active(
                mount.lease_id,
                task_id=task_id,
                artifact_digest=mount.digest,
                required_permissions=(permission,),
                runtime_profile="remote-mcp",
            )
            permissions[(permission.action, permission.resource)] = permission
            action_ref = f"mcp://{mount.mount_id}/{step.tool_name}"
            nodes.append({
                "id": step.id,
                "actionRef": action_ref,
                "version": str(mount.version),
                "inputBindings": dict(step.input_bindings),
                "outputSchema": {},
                "permissions": [permission.to_dict()],
                "timeoutMs": step.timeout_ms,
                "retry": {"maxAttempts": 1, "onlyIfIdempotent": True, "retryableErrors": []},
                "approval": "human",
                "idempotent": False,
            })
            for dependency in step.depends_on:
                edges.append({"source": dependency, "target": step.id})
            mount_ids.append(mount.mount_id)
            projected_actions.append(action_ref)

        ordered_permissions = tuple(
            permissions[key] for key in sorted(permissions)
        )
        spec = {
            "capabilityId": f"skill.mcp.{row.artifact_id}",
            "version": str(row.version),
            "inputsSchema": {},
            "preconditions": [],
            "effects": [item.to_dict() for item in ordered_permissions],
            "nodes": nodes,
            "edges": edges,
            "verifiers": [step.id for step in genome.mcp_steps if step.verifier],
            # MCP tool annotations are not yet trusted strongly enough to infer
            # reversibility/read-only behavior. Default to the strongest gate.
            "rollbackMode": "irreversible",
            "permissions": [item.to_dict() for item in ordered_permissions],
            "deadlineMs": sum(step.timeout_ms for step in genome.mcp_steps),
            "maxParallelism": 1,
            "riskClass": "critical",
            "skillBinding": {
                "skillDigest": row.content_digest,
                "mountIds": list(dict.fromkeys(mount_ids)),
                "bindingMode": "active-projection",
            },
        }
        skill_identity = f"{row.artifact_id}@{row.version}#{row.content_digest}"
        capability = create_artifact(
            artifact_id=f"skill.mcp.{row.artifact_id}",
            version=str(row.version),
            kind=ArtifactKind.CAPABILITY,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.LEARNED,
            spec=spec,
            created_at=self._created_at(),
            user_id=user_id,
            parent=skill_identity,
            task_origin=task_id,
            source_digest=row.content_digest,
        )
        await self._store.persist_artifact(capability)
        return SkillMcpActivation(
            capability=capability,
            skill_digest=row.content_digest,
            mount_ids=tuple(dict.fromkeys(mount_ids)),
            projected_actions=tuple(projected_actions),
        )


class SkillForge:
    def __init__(self, *, store: SqlCapabilityOsStore, clock_ms) -> None:
        self._store = store
        self._clock_ms = clock_ms

    def _created_at(self) -> str:
        seconds = int(self._clock_ms()) / 1000
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

    async def create(
        self,
        *,
        skill_id: str,
        version: str,
        task_id: str,
        genome: SkillGenome,
        parent: str | None = None,
        operator: str | None = None,
        user_id: str | None = None,
    ) -> CptrArtifact[Any]:
        if operator is not None and operator not in _SKILL_OPERATORS:
            raise ValueError("invalid Skill Genome evolutionary operator")
        artifact = create_artifact(
            artifact_id=skill_id,
            version=version,
            kind=ArtifactKind.SKILL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.LEARNED if parent else ArtifactOrigin.FORGE,
            spec=genome.to_spec(parent=parent, operator=operator),
            created_at=self._created_at(),
            user_id=user_id,
            task_origin=task_id,
            parent=parent,
        )
        await self._store.persist_artifact(artifact)
        return artifact

    async def mutate(
        self,
        content_digest: str,
        *,
        version: str,
        operator: str,
        changes: dict[str, Any],
    ) -> CptrArtifact[Any]:
        if operator not in _SKILL_OPERATORS:
            raise ValueError("invalid Skill Genome evolutionary operator")
        row = await self._store.get_artifact(content_digest)
        if row is None or row.kind != ArtifactKind.SKILL.value:
            raise KeyError("skill artifact not found")
        parent = self._artifact_from_row(row)
        genome = SkillGenome.from_spec(dict(row.spec or {}))
        allowed_changes = set(genome.__dataclass_fields__)
        unknown = set(changes) - allowed_changes
        if unknown:
            raise ValueError(f"unknown Skill Genome field: {sorted(unknown)[0]}")
        normalized: dict[str, Any] = {}
        tuple_fields = {
            "assumptions",
            "decomposition_strategy",
            "decision_rules",
            "evidence_policy",
            "tool_selection_heuristics",
            "stopping_conditions",
            "failure_recovery",
            "verification_requirements",
            "activation_domains",
            "activation_task_patterns",
            "activation_exclusions",
            "context_resources",
            "guardrail_metrics",
        }
        for key, value in changes.items():
            normalized[key] = tuple(value) if key in tuple_fields else value
        mutated = replace(genome, **normalized)
        return await self.create(
            skill_id=row.artifact_id,
            version=version,
            task_id=row.task_origin or "unknown-task",
            genome=mutated,
            parent=parent.identity,
            operator=operator,
            user_id=row.user_id,
        )

    async def promote(
        self,
        content_digest: str,
        *,
        evaluation: SkillEvaluationResult,
    ) -> None:
        if not evaluation.promotable:
            raise SkillEvaluationError("skill candidate lacks promotion evidence")
        row = await self._store.get_artifact(content_digest)
        if row is None or row.kind != ArtifactKind.SKILL.value:
            raise KeyError("skill artifact not found")
        current = ArtifactState(row.state)
        if current is ArtifactState.EPHEMERAL:
            target = ArtifactState.QUALIFIED
        elif current is ArtifactState.QUALIFIED:
            target = ArtifactState.LEARNED
        else:
            raise SkillEvaluationError("skill cannot be promoted from current state")
        await self._store.set_artifact_state(content_digest, state=target.value)

    @staticmethod
    def _artifact_from_row(row) -> CptrArtifact[Any]:
        return CptrArtifact(
            api_version="cptr.io/v1alpha1",
            kind=ArtifactKind(row.kind),
            metadata=ArtifactMetadata(
                id=row.artifact_id,
                version=row.version,
                owner=ArtifactOwner(row.owner),
                origin=ArtifactOrigin(row.origin),
                created_at=row.created_at,
                user_id=row.user_id,
                parent=row.parent_ref,
                task_origin=row.task_origin,
                source_digest=row.source_digest,
                content_digest=row.content_digest,
            ),
            compatibility=dict(row.compatibility or {}),
            spec=dict(row.spec or {}),
            state=ArtifactState(row.state),
        )
