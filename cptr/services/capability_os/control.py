"""Compact server-authoritative Capability OS control service."""
from __future__ import annotations

import inspect
import time
from dataclasses import asdict

from cptr.services.capability_os.authority import AuthorityBroker, AuthorityDenied, LeaseRequest
from cptr.services.capability_os.compiler import CapabilityCompiler, CapabilitySpec, DagEdge, DagNode, RetryPolicy
from cptr.services.capability_os.contracts import ArtifactKind, ArtifactState, CapabilityRequest
from cptr.services.capability_os.evidence import EvidenceService
from cptr.services.capability_os.evolution import (
    ChangeClass,
    EvolutionGate,
    ExperimentArm,
    ExperimentComparison,
    PromotionDecision,
)
from cptr.services.capability_os.evolution_engine import (
    EvolutionExperimentEngine,
    ExperimentArmName,
    ExperimentContext,
    ExperimentMode,
)
from cptr.services.capability_os.forge import CreateToolRequest, ToolForge
from cptr.services.capability_os.generated_executor import ProjectedGeneratedToolExecutor
from cptr.services.capability_os.mcp_fabric import AcquisitionGoal, McpFabric, McpQualification
from cptr.services.capability_os.mcp_remote import McpAcquisitionService, ProjectedMcpActionExecutor
from cptr.services.capability_os.policy import DenyAllAuthorityPolicyProvider
from cptr.services.capability_os.resolver import CapabilityResolver, ResolutionGoal
from cptr.services.capability_os.runtime import RuntimeBroker
from cptr.services.capability_os.skill_forge import (
    SkillEvaluationArm,
    SkillEvaluator,
    SkillForge,
    SkillGenome,
    SkillMcpActivator,
)
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.services.capability_os.tasks import CapabilityTaskCoordinator
from cptr.services.capability_os.vm import CapabilityVm


class CapabilityOsUnavailable(RuntimeError):
    pass


def _task(task):
    return {"taskId": task.task_id, "userId": task.user_id, "workspaceId": task.workspace_id,
            "source": task.source, "status": task.status, "active": bool(task.active),
            "executionAllowed": bool(task.execution_allowed)}


def _artifact(row):
    return {"apiVersion": "cptr.io/v1alpha1", "kind": row.kind,
            "metadata": {"id": row.artifact_id, "version": row.version, "owner": row.owner,
                         "userId": row.user_id, "origin": row.origin, "createdAt": row.created_at,
                         "parent": row.parent_ref, "taskOrigin": row.task_origin,
                         "sourceDigest": row.source_digest, "contentDigest": row.content_digest},
            "compatibility": dict(row.compatibility or {}), "spec": dict(row.spec or {}),
            "state": row.state}


def _requests(values) -> tuple[CapabilityRequest, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("capability requests must be an array")
    return tuple(item if isinstance(item, CapabilityRequest) else CapabilityRequest.from_dict(item)
                 for item in values)


def _candidate(item):
    return {"artifactId": item.artifact_id, "version": item.version, "kind": item.kind,
            "contentDigest": item.content_digest, "state": item.state, "origin": item.origin,
            "taskOrigin": item.task_origin, "capabilities": [x.to_dict() for x in item.capabilities],
            "score": item.score}


def _skill_arm(p):
    if not isinstance(p, dict):
        raise ValueError("skill evaluation arm must be an object")
    return SkillEvaluationArm(
        runs=int(p.get("runs") or 0),
        successes=int(p.get("successes") or 0),
        regressions=int(p.get("regressions") or 0),
        policy_violations=int(p.get("policyViolations") or 0),
        model_id=str(p.get("modelId") or ""),
        reasoning_effort=str(p.get("reasoningEffort") or ""),
        tool_permission_fingerprint=str(p.get("toolPermissionFingerprint") or ""),
        resource_budget_fingerprint=str(p.get("resourceBudgetFingerprint") or ""),
        task_distribution_fingerprint=str(p.get("taskDistributionFingerprint") or ""),
        mean_tokens=float(p.get("meanTokens") or 0.0),
        mean_tool_calls=float(p.get("meanToolCalls") or 0.0),
    )


def _goal(task_id, p):
    return AcquisitionGoal(task_id=task_id, goal=str(p.get("goal") or ""),
                           required=tuple(map(str, p.get("required") or ())),
                           optional=tuple(map(str, p.get("optional") or ())),
                           forbidden=tuple(map(str, p.get("forbidden") or ())),
                           data_classification=str(p.get("dataClassification") or "private"))


def _capability_spec(p):
    nodes = tuple(DagNode(id=str(n.get("id") or ""), action_ref=str(n.get("actionRef") or ""),
                          version=str(n.get("version") or ""), input_bindings=dict(n.get("inputBindings") or {}),
                          output_schema=dict(n.get("outputSchema") or {}),
                          preconditions=tuple(n.get("preconditions") or ()),
                          postconditions=tuple(n.get("postconditions") or ()),
                          permissions=_requests(n.get("permissions") or ()),
                          timeout_ms=int(n.get("timeoutMs") or 30000),
                          retry=RetryPolicy(max_attempts=int((n.get("retry") or {}).get("maxAttempts") or 1),
                                            only_if_idempotent=bool((n.get("retry") or {}).get("onlyIfIdempotent", True)),
                                            retryable_errors=tuple(map(str, (n.get("retry") or {}).get("retryableErrors") or ()))),
                          compensation_action_ref=n.get("compensationActionRef"),
                          approval=str(n.get("approval") or "none"), idempotent=bool(n.get("idempotent", False)))
                  for n in p.get("nodes") or ())
    edges = tuple(DagEdge(str(e.get("source") or ""), str(e.get("target") or "")) if isinstance(e, dict)
                  else DagEdge(str(e[0]), str(e[1])) for e in p.get("edges") or ())
    return CapabilitySpec(capability_id=str(p.get("capabilityId") or p.get("id") or ""),
                          version=str(p.get("version") or ""), inputs_schema=dict(p.get("inputsSchema") or {}),
                          preconditions=tuple(p.get("preconditions") or ()), effects=_requests(p.get("effects") or ()),
                          nodes=nodes, edges=edges, verifiers=tuple(map(str, p.get("verifiers") or ())),
                          rollback_mode=str(p.get("rollbackMode") or "partial"), permissions=_requests(p.get("permissions") or ()),
                          deadline_ms=int(p.get("deadlineMs") or 30000), max_parallelism=int(p.get("maxParallelism") or 1),
                          risk_class=str(p.get("riskClass") or "read"))


class CapabilityOsControlService:
    def __init__(self, *, store: SqlCapabilityOsStore, tasks: CapabilityTaskCoordinator,
                 authority: AuthorityBroker, resolver: CapabilityResolver, forge: ToolForge,
                 compiler: CapabilityCompiler, evidence: EvidenceService, evolution: EvolutionGate,
                 runtime: RuntimeBroker, fabric: McpFabric, action_executor=None, mcp_connector=None,
                 mcp_acquisition: McpAcquisitionService | None = None, credential_broker=None,
                 skill_activator: SkillMcpActivator | None = None,
                 skill_forge: SkillForge | None = None,
                 skill_evaluator: SkillEvaluator | None = None,
                 evolution_engine: EvolutionExperimentEngine | None = None,
                 evolution_approval_verifier=None,
                 policy_provider=None, clock_ms=lambda: int(time.time() * 1000)):
        self.store, self.tasks, self.authority, self.resolver = store, tasks, authority, resolver
        self.forge_impl, self.compiler, self.evidence, self.evolution = forge, compiler, evidence, evolution
        self.runtime, self.fabric = runtime, fabric
        self.action_executor, self.mcp_connector = action_executor, mcp_connector
        self.mcp_acquisition = mcp_acquisition
        self.credential_broker = credential_broker
        self.skill_activator = skill_activator
        self.skill_forge = skill_forge or SkillForge(store=store, clock_ms=clock_ms)
        self.skill_evaluator = skill_evaluator or SkillEvaluator()
        self.evolution_engine = evolution_engine or EvolutionExperimentEngine(
            store=store,
            evidence=evidence,
            gate=evolution,
        )
        self.evolution_approval_verifier = evolution_approval_verifier
        self.policy_provider = policy_provider or DenyAllAuthorityPolicyProvider()
        self.clock_ms = clock_ms

    async def _revoke_lease(self, lease_id: str) -> bool:
        if self.credential_broker is not None:
            await self.credential_broker.revoke_lease(lease_id)
        return await self.authority.revoke(lease_id)

    async def close_task(self, task_id: str) -> dict[str, int]:
        result = await self.fabric.close_task(task_id)
        if self.credential_broker is not None:
            await self.credential_broker.revoke_task(task_id)
        return result

    async def _visible(self, user_id, task_id, digest, *, mutable=False):
        row = await self.store.get_artifact(digest, user_id=user_id, include_global=not mutable)
        if row is None or (row.state == ArtifactState.EPHEMERAL.value and row.task_origin != task_id):
            raise KeyError("Capability OS artifact not found")
        return row

    async def inspect(self, *, user_id, task_id, artifact_digest=None, limit=50):
        task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
        artifact_row = await self._visible(user_id, task_id, artifact_digest) if artifact_digest else None
        artifact = _artifact(artifact_row) if artifact_row is not None else None
        artifact_reputation = None
        if (
            artifact_row is not None
            and artifact_row.kind == ArtifactKind.MCP_ADAPTER.value
            and self.mcp_acquisition is not None
        ):
            server_id = str((artifact_row.spec or {}).get("serverId") or "").strip()
            if server_id:
                artifact_reputation = (
                    await self.mcp_acquisition.reputation_snapshot(
                        user_id=user_id,
                        server_id=server_id,
                    )
                ).to_api()
        rows = await self.store.list_artifacts(user_id=user_id, include_global=True, limit=max(1, min(int(limit), 100)))
        leases = await self.store.list_active_leases(task_id, now_ms=int(self.clock_ms()))
        mounts = await self.store.list_active_mounts(task_id)
        evidence = await self.store.list_evidence(task_id, limit=max(1, min(int(limit), 100)))
        evidence_chain = await self.evidence.verify_chain(task_id)
        visible_rows = [
            row for row in rows
            if row.state != ArtifactState.EPHEMERAL.value or row.task_origin == task_id
        ]
        return {"task": _task(task), "artifact": artifact, "artifactReputation": artifact_reputation,
                "artifacts": [_artifact(r) for r in visible_rows],
                "activeLeases": [{"leaseId": r.lease_id, "artifactDigest": r.artifact_digest,
                                  "permissions": list(r.permissions or []), "runtimeProfile": r.runtime_profile,
                                  "expiresAtMs": int(r.expires_at_ms)} for r in leases],
                "activeMounts": [{"mountId": r.mount_id, "serverId": r.server_id,
                                  "projectedTools": list(r.projected_tools or [])} for r in mounts],
                "evidence": [{"evidenceId": r.evidence_id, "sequence": int(r.sequence),
                              "previousDigest": r.previous_digest, "digest": r.digest,
                              "runId": r.run_id, "kind": r.kind,
                              "producerIdentity": r.producer_identity,
                              "artifactDigest": r.artifact_digest, "leaseId": r.lease_id,
                              "claims": dict(r.claims or {})} for r in evidence],
                "evidenceChain": evidence_chain,
                "runtime": self.runtime.production_snapshot()}

    async def resolve(self, *, user_id, task_id, required, optional=(), forbidden=()):
        task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
        result = await self.resolver.resolve(ResolutionGoal(task_id=task_id, required=tuple(required),
                                                           optional=tuple(optional), forbidden=tuple(forbidden)),
                                             user_id=user_id)
        return {"task": _task(task), "candidates": [_candidate(x) for x in result.candidates],
                "missing": [x.to_dict() for x in result.missing], "acquisitionModes": list(result.acquisition_modes)}

    async def forge(self, *, user_id, task_id, operation, payload):
        operation = operation.strip().lower()
        if operation == "skill-export":
            task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
            if set(payload) - {"contentDigest"}:
                raise ValueError("skill-export accepts only contentDigest")
            digest = str(payload.get("contentDigest") or "").strip()
            row = await self._visible(user_id, task_id, digest)
            if row.kind != ArtifactKind.SKILL.value:
                raise ValueError("skill-export requires a Skill artifact")
            exported = await self.skill_forge.export_bundle(digest)
            evidence = await self.evidence.record(
                task_id=task_id,
                kind="skill.export",
                producer_identity="capability-os-control",
                claims={
                    "bundleDigest": exported["bundleDigest"],
                    "sourceContentDigest": digest,
                    "format": "cptr.io/skill-bundle/v1",
                },
                artifact_digest=digest,
            )
            return {
                "task": _task(task),
                **exported,
                "evidenceId": evidence.evidence_id,
            }
        if operation == "skill-import":
            task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
            if set(payload) - {"bundle"}:
                raise ValueError("skill-import accepts only bundle")
            bundle = payload.get("bundle")
            if not isinstance(bundle, dict):
                raise ValueError("skill-import requires a bundle object")
            artifact, bundle_digest, source_content_digest = await self.skill_forge.import_bundle(
                bundle=dict(bundle),
                task_id=task_id,
                user_id=user_id,
            )
            evidence = await self.evidence.record(
                task_id=task_id,
                kind="skill.import",
                producer_identity="capability-os-control",
                claims={
                    "bundleDigest": bundle_digest,
                    "sourceContentDigest": source_content_digest,
                    "format": "cptr.io/skill-bundle/v1",
                },
                artifact_digest=artifact.metadata.content_digest,
            )
            return {
                "task": _task(task),
                "artifact": artifact.to_dict(),
                "bundleDigest": bundle_digest,
                "evidenceId": evidence.evidence_id,
            }
        if operation == "skill-create":
            task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
            genome_payload = payload.get("genome")
            if not isinstance(genome_payload, dict):
                raise ValueError("skill-create requires a genome object")
            artifact = await self.skill_forge.create(
                skill_id=str(payload.get("skillId") or ""),
                version=str(payload.get("version") or ""),
                task_id=task_id,
                genome=SkillGenome.from_spec(genome_payload),
                user_id=user_id,
            )
            return {"task": _task(task), "artifact": artifact.to_dict()}
        if operation == "skill-mutate":
            task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
            digest = str(payload.get("contentDigest") or "").strip()
            row = await self._visible(user_id, task_id, digest, mutable=True)
            if row.kind != ArtifactKind.SKILL.value:
                raise ValueError("skill-mutate requires a Skill artifact")
            changes = payload.get("changes")
            if not isinstance(changes, dict):
                raise ValueError("skill-mutate requires a changes object")
            artifact = await self.skill_forge.mutate(
                digest,
                version=str(payload.get("version") or ""),
                operator=str(payload.get("operator") or ""),
                changes=changes,
            )
            return {"task": _task(task), "artifact": artifact.to_dict()}
        if operation in {"skill-evaluate", "skill-promote"}:
            task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
            digest = str(payload.get("contentDigest") or "").strip()
            row = await self._visible(user_id, task_id, digest, mutable=True)
            if row.kind != ArtifactKind.SKILL.value:
                raise ValueError("skill evaluation requires a Skill artifact")
            evaluation = self.skill_evaluator.compare(
                baseline=_skill_arm(payload.get("baseline")),
                candidate=_skill_arm(payload.get("candidate")),
            )
            evidence = await self.evidence.record(
                task_id=task_id,
                kind="skill.evaluation",
                producer_identity="capability-os-control",
                claims={
                    "promotable": evaluation.promotable,
                    "reason": evaluation.reason,
                    "successDelta": evaluation.success_delta,
                    "tokenDelta": evaluation.token_delta,
                    "toolCallDelta": evaluation.tool_call_delta,
                },
                artifact_digest=digest,
            )
            result = {
                "task": _task(task),
                "evaluation": asdict(evaluation),
                "evidenceId": evidence.evidence_id,
                "promoted": False,
            }
            if operation == "skill-promote" and evaluation.promotable:
                await self.skill_forge.promote(digest, evaluation=evaluation)
                updated = await self._visible(user_id, task_id, digest, mutable=True)
                promotion = await self.evidence.record(
                    task_id=task_id,
                    kind="skill.promotion",
                    producer_identity="capability-os-control",
                    claims={
                        "evaluationEvidenceId": evidence.evidence_id,
                        "targetState": updated.state,
                    },
                    artifact_digest=digest,
                )
                result.update({
                    "promoted": True,
                    "artifactState": updated.state,
                    "promotionEvidenceId": promotion.evidence_id,
                })
            return result
        if operation == "activate-skill-mcp":
            task = await self.tasks.require_executable(user_id=user_id, task_id=task_id)
            if self.skill_activator is None:
                raise CapabilityOsUnavailable("Capability OS Skill MCP activator is not configured")
            if set(payload) - {"skillDigest"}:
                raise ValueError("Skill MCP activation accepts only skillDigest")
            skill_digest = str(payload.get("skillDigest") or "").strip()
            if not skill_digest:
                raise ValueError("Skill MCP activation requires skillDigest")
            activation = await self.skill_activator.activate(
                skill_digest=skill_digest,
                user_id=user_id,
                task_id=task_id,
            )
            evidence = await self.evidence.record(
                task_id=task_id,
                kind="skill.mcp.activation",
                producer_identity="capability-os-control",
                claims={
                    "skillDigest": activation.skill_digest,
                    "capabilityDigest": activation.capability.metadata.content_digest,
                    "mountIds": list(activation.mount_ids),
                    "projectedActions": list(activation.projected_actions),
                    "riskClass": activation.capability.spec.get("riskClass"),
                    "rollbackMode": activation.capability.spec.get("rollbackMode"),
                },
                artifact_digest=activation.capability.metadata.content_digest,
            )
            return {
                "task": _task(task),
                "artifact": activation.capability.to_dict(),
                "skillDigest": activation.skill_digest,
                "mountIds": list(activation.mount_ids),
                "projectedActions": list(activation.projected_actions),
                "evidenceId": evidence.evidence_id,
            }
        if operation == "create":
            task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
            draft = await self.forge_impl.create(CreateToolRequest(
                tool_id=str(payload.get("toolId") or ""), version=str(payload.get("version") or ""), task_id=task_id,
                runtime_class=str(payload.get("runtimeClass") or ""), entrypoint=str(payload.get("entrypoint") or ""),
                files={str(k): str(v) for k, v in dict(payload.get("files") or {}).items()},
                requested_capabilities=_requests(payload.get("requestedCapabilities") or ()), user_id=user_id,
                input_schema=dict(payload.get("inputSchema") or {}), output_schema=dict(payload.get("outputSchema") or {}),
                resources=dict(payload.get("resources") or {}), deterministic=payload.get("deterministic"),
                idempotent=payload.get("idempotent"), reversibility=str(payload.get("reversibility") or "unknown")))
            return {"task": _task(task), "artifact": draft.artifact.to_dict(), "sourceDigest": draft.source_digest}
        if operation == "inspect":
            task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
            digest = str(payload.get("contentDigest") or "").strip()
            row = await self._visible(user_id, task_id, digest, mutable=True)
            if row.kind != ArtifactKind.TOOL.value:
                raise ValueError("Tool Forge inspect requires a Tool artifact")
            return {"task": _task(task), "artifact": _artifact(row)}
        if operation == "supply-chain":
            task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
            if set(payload) - {"contentDigest", "documentDigest"}:
                raise ValueError("Tool Forge supply-chain accepts only contentDigest and documentDigest")
            digest = str(payload.get("contentDigest") or "").strip()
            document_digest = str(payload.get("documentDigest") or "").strip()
            row = await self._visible(user_id, task_id, digest, mutable=True)
            if row.kind != ArtifactKind.TOOL.value:
                raise ValueError("Tool Forge supply-chain requires a Tool artifact")
            if not document_digest.startswith("sha256:"):
                raise ValueError("Tool Forge supply-chain requires a documentDigest")
            evidence_rows = await self.store.list_artifact_evidence(digest, limit=1000)
            trusted = False
            for evidence in evidence_rows:
                if evidence.kind != "tool.build" or evidence.producer_identity != "capability-os-control":
                    continue
                supply_chain = (evidence.claims or {}).get("supplyChain")
                if not isinstance(supply_chain, dict):
                    continue
                if any(
                    isinstance(ref, dict) and str(ref.get("digest") or "") == document_digest
                    for ref in supply_chain.values()
                ):
                    trusted = True
                    break
            if not trusted:
                raise KeyError("Tool Forge supply-chain document not found")
            return {
                "task": _task(task),
                "artifactDigest": digest,
                "documentDigest": document_digest,
                "document": self.forge_impl.get_supply_chain_document(document_digest),
            }
        task = await self.tasks.require_executable(user_id=user_id, task_id=task_id)
        digest = str(payload.get("contentDigest") or "")
        row = await self._visible(user_id, task_id, digest, mutable=True)
        if row.kind != ArtifactKind.TOOL.value:
            raise ValueError("Tool Forge operation requires a Tool artifact")
        if operation == "modify":
            draft = await self.forge_impl.modify(
                digest,
                version=str(payload.get("version") or ""),
                files={str(k): str(v) for k, v in dict(payload.get("files") or {}).items()},
            )
            return {"task": _task(task), "artifact": draft.artifact.to_dict(), "sourceDigest": draft.source_digest}
        if operation == "fork":
            draft = await self.forge_impl.fork(
                digest,
                tool_id=str(payload.get("toolId") or ""),
                version=str(payload.get("version") or ""),
            )
            return {"task": _task(task), "artifact": draft.artifact.to_dict(), "sourceDigest": draft.source_digest}
        if operation == "build":
            resources = dict(row.spec.get("resources") or {})
            lease, automatic = await self._lease(
                task=task, artifact_digest=digest, workload_id=f"tool-build:{row.artifact_id}",
                permissions=(CapabilityRequest("runtime.build", f"artifact:{digest}"),),
                runtime_profile=str((row.spec.get("runtime") or {}).get("class") or "gvisor"),
                resource_limits=resources,
            )
            try:
                try:
                    build = await self.forge_impl.build(digest, lease=lease)
                except Exception as exc:
                    await self.evidence.record(
                        task_id=task_id, kind="tool.build.failure", producer_identity="capability-os-control",
                        claims={"toolId": row.artifact_id, "errorType": exc.__class__.__name__},
                        artifact_digest=digest, lease_id=lease.lease_id,
                    )
                    raise
                evidence = await self.evidence.record(
                    task_id=task_id, kind="tool.build", producer_identity="capability-os-control",
                    claims={"toolId": row.artifact_id, "sourceDigest": row.source_digest,
                            "buildArtifactDigest": build.artifact_digest, "runtimeClass": build.runtime_class,
                            "attestation": build.attestation, "supplyChain": build.supply_chain},
                    run_id=build.build_id, artifact_digest=digest, lease_id=lease.lease_id,
                )
                return {"task": _task(task), "build": asdict(build),
                        "evidenceId": evidence.evidence_id, "automaticLease": automatic}
            finally:
                if automatic:
                    await self._revoke_lease(lease.lease_id)
        if operation == "run":
            resources = dict(row.spec.get("resources") or {})
            lease, automatic = await self._lease(
                task=task, artifact_digest=digest, workload_id=f"tool-run:{row.artifact_id}",
                permissions=(CapabilityRequest("runtime.run", f"artifact:{digest}"),),
                runtime_profile=str((row.spec.get("runtime") or {}).get("class") or "gvisor"),
                resource_limits=resources,
            )
            try:
                try:
                    run = await self.forge_impl.run(
                        digest,
                        inputs=dict(payload.get("inputs") or {}),
                        lease=lease,
                        timeout_ms=(int(payload["timeoutMs"]) if "timeoutMs" in payload else None),
                    )
                except Exception as exc:
                    await self.evidence.record(
                        task_id=task_id, kind="tool.run.failure", producer_identity="capability-os-control",
                        claims={"toolId": row.artifact_id, "errorType": exc.__class__.__name__},
                        artifact_digest=digest, lease_id=lease.lease_id,
                    )
                    raise
                evidence = await self.evidence.record(
                    task_id=task_id, kind="tool.run", producer_identity="capability-os-control",
                    claims={"toolId": row.artifact_id, "sourceDigest": row.source_digest,
                            "executionArtifactDigest": run.execution_artifact_digest,
                            "runtimeClass": run.runtime_class, "attestation": run.attestation},
                    run_id=run.run_id, artifact_digest=digest, lease_id=lease.lease_id,
                )
                return {"task": _task(task), "run": asdict(run),
                        "evidenceId": evidence.evidence_id, "automaticLease": automatic}
            finally:
                if automatic:
                    await self._revoke_lease(lease.lease_id)
        if operation == "persist":
            await self.forge_impl.persist(digest, target_state=ArtifactState(str(payload.get("targetState") or "")),
                                          evidence_ids=tuple(map(str, payload.get("evidenceIds") or ())))
        elif operation == "destroy":
            await self.forge_impl.destroy(digest)
        else:
            raise ValueError("unsupported Tool Forge operation")
        row = await self.store.get_artifact(digest, user_id=user_id, include_global=False)
        return {"task": _task(task), "artifact": _artifact(row)}

    async def _lease(self, *, task, artifact_digest, workload_id, permissions, runtime_profile,
                     lease_id=None, approval_id=None, resource_limits=None,
                     network_destinations=(), credential_names=()):
        execution_context = {
            "userId": task.user_id,
            "workspaceId": task.workspace_id,
            "taskSource": task.source,
        }
        if lease_id:
            return (await self.authority.require_active(
                lease_id, task_id=task.task_id, artifact_digest=artifact_digest,
                required_permissions=tuple(permissions), runtime_profile=runtime_profile,
                resource_limits=dict(resource_limits or {}), network_destinations=tuple(network_destinations),
                credential_names=tuple(credential_names), execution_context=execution_context), False)
        policy = await self.policy_provider.resolve(task=task, artifact_digest=artifact_digest,
                                                    workload_id=workload_id)
        if policy is None:
            raise AuthorityDenied("no standing authority policy covers this task")
        lease = await self.authority.issue(
            LeaseRequest(task_id=task.task_id, workload_id=workload_id, artifact_digest=artifact_digest,
                         permissions=tuple(permissions), runtime_profile=runtime_profile,
                         requested_lease_ms=policy.max_lease_ms, resource_limits=dict(resource_limits or {}),
                         network_destinations=tuple(network_destinations), credential_names=tuple(credential_names),
                         execution_context=execution_context),
            policy=policy, approval_id=approval_id)
        return lease, True

    async def _invoke_packaged_mcp(
        self,
        *,
        artifact,
        tool_name: str,
        arguments: dict,
        task_id: str,
        timeout_ms: int,
    ):
        if self.mcp_acquisition is None:
            raise CapabilityOsUnavailable("Capability OS MCP acquisition service is not configured")
        if not artifact.user_id:
            raise PermissionError("packaged MCP adapter is missing user ownership")
        task = await self.tasks.require_executable(user_id=artifact.user_id, task_id=task_id)
        resources = dict(artifact.spec.get("resources") or {})
        lease, automatic = await self._lease(
            task=task,
            artifact_digest=artifact.content_digest,
            workload_id=f"tool-run:mcp-package:{artifact.artifact_id}",
            permissions=(CapabilityRequest("runtime.run", f"artifact:{artifact.content_digest}"),),
            runtime_profile="gvisor",
            resource_limits=resources,
        )
        try:
            result = await self.mcp_acquisition.invoke_packaged(
                artifact,
                tool_name=tool_name,
                arguments=dict(arguments),
                lease=lease,
                timeout_ms=int(timeout_ms),
            )
            await self.evidence.record(
                task_id=task_id,
                kind="mcp.package.invoke",
                producer_identity="capability-os-control",
                claims={
                    "serverId": str((artifact.spec or {}).get("serverId") or ""),
                    "tool": str(tool_name),
                    "transport": "stdio-gvisor",
                    "attestation": dict(result.metadata.get("attestation") or {}),
                },
                artifact_digest=artifact.content_digest,
                lease_id=lease.lease_id,
            )
            return result
        finally:
            if automatic:
                await self._revoke_lease(lease.lease_id)

    async def execute(self, *, user_id, task_id, capability_digest, lease_id, spec, inputs, approval_id=None):
        task = await self.tasks.require_executable(user_id=user_id, task_id=task_id)
        if self.action_executor is None:
            raise CapabilityOsUnavailable("Capability OS action executor is not configured")
        row = await self._visible(user_id, task_id, capability_digest)
        if row.kind != ArtifactKind.CAPABILITY.value:
            raise ValueError("execute requires a Capability artifact")
        stored = dict(row.spec or {})
        recipe = stored.get("capability") if isinstance(stored.get("capability"), dict) else stored
        compiled = self.compiler.compile(_capability_spec(recipe))
        lease, automatic = await self._lease(
            task=task, artifact_digest=capability_digest,
            workload_id=f"capability:{row.artifact_id}", permissions=compiled.permissions,
            runtime_profile="cptr-vm", lease_id=lease_id, approval_id=approval_id)
        executor = ProjectedGeneratedToolExecutor(
            base_executor=self.action_executor,
            store=self.store,
            authority=self.authority,
            forge=self.forge_impl,
            evidence=self.evidence,
            policy_provider=self.policy_provider,
            clock_ms=self.clock_ms,
        )
        if self.mcp_connector is not None:
            executor = ProjectedMcpActionExecutor(
                base_executor=executor,
                store=self.store,
                authority=self.authority,
                connector=self.mcp_connector,
                packaged_invoke=self._invoke_packaged_mcp,
                credential_broker=self.credential_broker,
                evidence=self.evidence,
            )
        try:
            result = await CapabilityVm(executor=executor, evidence=self.evidence, clock_ms=self.clock_ms).execute(
                task_id=task_id, capability_digest=capability_digest, compiled=compiled, lease=lease,
                inputs=dict(inputs), approval_id=approval_id)
        finally:
            if automatic:
                await self._revoke_lease(lease.lease_id)
        return {"task": _task(task), "result": asdict(result), "automaticLease": automatic}

    async def acquire(self, *, user_id, task_id, operation, payload):
        operation = operation.strip().lower()
        task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
        if operation == "release":
            mount_id = str(payload.get("mountId") or "")
            row = await self.store.get_mcp_mount(mount_id)
            if row is None or row.task_id != task_id:
                raise KeyError("MCP mount not found")
            if self.mcp_connector is not None:
                result = self.mcp_connector.release(mount_id=mount_id, task_id=task_id)
                if inspect.isawaitable(result):
                    await result
            return {"task": _task(task), "released": await self.fabric.release(mount_id, task_id=task_id)}

        gp = payload.get("goal")
        if not isinstance(gp, dict):
            raise ValueError("acquire requires a goal object")
        goal = _goal(task_id, gp)

        if operation in {"discover", "qualify"}:
            task = await self.tasks.require_executable(user_id=user_id, task_id=task_id)
            if self.mcp_acquisition is None:
                raise CapabilityOsUnavailable("Capability OS MCP acquisition service is not configured")
            query = str(payload.get("query") or "").strip()
            if not query:
                raise ValueError("MCP acquisition requires a discovery query")
            acquired = await self.mcp_acquisition.discover_and_qualify(
                user_id=user_id, task_id=task_id, goal=goal, query=query
            )
            return {
                "task": _task(task),
                "candidates": [
                    {"artifactDigest": item.artifact_digest, "serverId": item.server_id,
                     "version": item.version, "state": item.state, "eligible": item.eligible,
                     "reasons": list(item.reasons), "projectedMatch": list(item.projected_match)}
                    for item in acquired
                ],
            }

        if operation != "mount":
            raise ValueError("unsupported MCP acquisition operation")
        task = await self.tasks.require_executable(user_id=user_id, task_id=task_id)
        if self.mcp_connector is None or self.mcp_acquisition is None:
            raise CapabilityOsUnavailable("Capability OS MCP connector is not configured")
        digest = str(payload.get("artifactDigest") or "").strip()
        if not digest:
            raise ValueError("MCP mount requires an acquired artifactDigest")
        row = await self._visible(user_id, task_id, digest, mutable=True)
        qualification_evidence_id = None
        if (
            row.state == ArtifactState.EPHEMERAL.value
            and self.mcp_acquisition.is_packaged_adapter(row)
        ):
            resources = dict(row.spec.get("resources") or {})
            package_lease, package_automatic = await self._lease(
                task=task,
                artifact_digest=row.content_digest,
                workload_id=f"tool-run:mcp-qualify:{row.artifact_id}",
                permissions=(
                    CapabilityRequest("runtime.run", f"artifact:{row.content_digest}"),
                ),
                runtime_profile="gvisor",
                resource_limits=resources,
            )
            try:
                packaged = await self.mcp_acquisition.qualify_packaged(
                    row,
                    goal=goal,
                    lease=package_lease,
                )
                evidence = await self.evidence.record(
                    task_id=task_id,
                    kind="mcp.package.qualification",
                    producer_identity="capability-os-control",
                    claims={
                        "quarantinedArtifactDigest": row.content_digest,
                        "qualifiedArtifactDigest": packaged.artifact_digest,
                        "protocolVersion": packaged.protocol_version,
                        "serverName": packaged.server_name,
                        "serverVersion": packaged.server_version,
                        "projectedMatch": list(packaged.projected_match),
                        "transport": "stdio-gvisor",
                        "attestation": dict(packaged.attestation),
                    },
                    artifact_digest=row.content_digest,
                    lease_id=package_lease.lease_id,
                )
                qualification_evidence_id = evidence.evidence_id
                updated = await self.store.set_artifact_state(
                    packaged.artifact_digest,
                    state=ArtifactState.QUALIFIED.value,
                )
                if not updated:
                    raise RuntimeError("packaged MCP qualification artifact disappeared")
                row = await self._visible(
                    user_id,
                    task_id,
                    packaged.artifact_digest,
                    mutable=True,
                )
            finally:
                if package_automatic:
                    await self._revoke_lease(package_lease.lease_id)
        if (
            row.state == ArtifactState.EPHEMERAL.value
            and self.mcp_acquisition.is_authenticated_remote(row)
        ):
            remote_url, logical_name, _consumer = self.mcp_acquisition.remote_auth_details(row)
            server_id = str((row.spec or {}).get("serverId") or "").strip()
            permissions = _requests((row.spec or {}).get("permissions") or ())
            qualification_lease, qualification_automatic = await self._lease(
                task=task,
                artifact_digest=row.content_digest,
                workload_id=f"mcp-qualify:{server_id}",
                permissions=permissions,
                runtime_profile="remote-mcp",
                approval_id=payload.get("approvalId"),
                network_destinations=(remote_url,),
                credential_names=(logical_name,),
            )
            try:
                authenticated = await self.mcp_acquisition.qualify_authenticated_remote(
                    row,
                    goal=goal,
                    lease=qualification_lease,
                )
                evidence = await self.evidence.record(
                    task_id=task_id,
                    kind="mcp.remote.qualification",
                    producer_identity="capability-os-control",
                    claims={
                        "quarantinedArtifactDigest": row.content_digest,
                        "qualifiedArtifactDigest": authenticated.artifact_digest,
                        "protocolVersion": authenticated.observation.protocol_version,
                        "serverName": authenticated.observation.server_name,
                        "serverVersion": authenticated.observation.server_version,
                        "projectedMatch": list(authenticated.projected_match),
                        "transport": "streamable-http",
                        "authentication": "credential-brokered-bearer",
                    },
                    artifact_digest=row.content_digest,
                    lease_id=qualification_lease.lease_id,
                )
                qualification_evidence_id = evidence.evidence_id
                updated = await self.store.set_artifact_state(
                    authenticated.artifact_digest,
                    state=ArtifactState.QUALIFIED.value,
                )
                if not updated:
                    raise RuntimeError("authenticated remote MCP qualification artifact disappeared")
                row = await self._visible(
                    user_id,
                    task_id,
                    authenticated.artifact_digest,
                    mutable=True,
                )
            finally:
                if qualification_automatic:
                    await self._revoke_lease(qualification_lease.lease_id)

        authenticated_remote = self.mcp_acquisition.is_authenticated_remote(row)
        if authenticated_remote:
            remote_url, logical_name, _consumer = self.mcp_acquisition.remote_auth_details(row)
            server_id = str((row.spec or {}).get("serverId") or "").strip()
            permissions = _requests((row.spec or {}).get("permissions") or ())
            lease, automatic = await self._lease(
                task=task,
                artifact_digest=row.content_digest,
                workload_id=f"mcp:{server_id}",
                permissions=permissions,
                runtime_profile="remote-mcp",
                lease_id=payload.get("leaseId"),
                approval_id=payload.get("approvalId"),
                network_destinations=(remote_url,),
                credential_names=(logical_name,),
            )
            try:
                candidate, remote_url = await self.mcp_acquisition.require_mount_candidate(
                    row,
                    goal=goal,
                    lease=lease,
                )
                qualification = self.fabric.qualify(candidate)
                q = {
                    "eligible": qualification.eligible,
                    "reasons": list(qualification.reasons),
                    "utilityScore": (
                        self.fabric.utility_score(candidate) if qualification.eligible else None
                    ),
                }
                mount = await self.fabric.mount(
                    goal=goal,
                    qualification=McpQualification(candidate=candidate, eligible=True, reasons=()),
                    lease=lease,
                )
            except Exception:
                if automatic:
                    await self._revoke_lease(lease.lease_id)
                raise
        else:
            candidate, remote_url = await self.mcp_acquisition.require_mount_candidate(row, goal=goal)
            qualification = self.fabric.qualify(candidate)
            q = {"eligible": qualification.eligible, "reasons": list(qualification.reasons),
                 "utilityScore": self.fabric.utility_score(candidate) if qualification.eligible else None}
            lease, automatic = await self._lease(
                task=task, artifact_digest=candidate.digest, workload_id=f"mcp:{candidate.server_id}",
                permissions=candidate.permissions, runtime_profile="remote-mcp",
                lease_id=payload.get("leaseId"), approval_id=payload.get("approvalId"),
                network_destinations=((remote_url,) if remote_url else ()))
            try:
                mount = await self.fabric.mount(
                    goal=goal,
                    qualification=McpQualification(candidate=candidate, eligible=True, reasons=()),
                    lease=lease,
                )
            except Exception:
                if automatic:
                    await self._revoke_lease(lease.lease_id)
                raise
        return {"task": _task(task), "qualification": q,
                "mount": {"mountId": mount.mount_id, "serverId": mount.server_id, "version": mount.version,
                          "digest": mount.digest, "state": mount.state.value, "leaseId": mount.lease_id,
                          "projectedTools": list(mount.projected_tools), "transportKind": mount.transport_kind},
                "qualificationEvidenceId": qualification_evidence_id,
                "automaticLease": automatic}

    async def _reflect_experiment(
        self,
        *,
        user_id: str,
        task_id: str,
        task,
        artifact_digest: str | None,
        experiment: dict,
        promotion_target_state: str | None,
        owner_approval_id: str | None,
    ):
        operation = str(experiment.get("operation") or "").strip().lower()
        if not operation:
            raise ValueError("experiment operation is required")
        allowed_fields = {
            "create": {
                "operation",
                "changeClass",
                "mode",
                "hypothesis",
                "controlArtifactDigest",
                "context",
                "minRunsPerArm",
            },
            "observe": {
                "operation",
                "experimentId",
                "arm",
                "sourceEvidenceId",
            },
            "evaluate": {"operation", "experimentId"},
            "status": {"operation", "experimentId"},
            "cancel": {"operation", "experimentId", "reason"},
            "promote": {"operation", "experimentId"},
        }
        if operation not in allowed_fields:
            raise ValueError("unsupported evolution experiment operation")
        unknown = set(experiment) - allowed_fields[operation]
        if unknown:
            raise ValueError(f"unknown evolution experiment field: {sorted(unknown)[0]}")

        if operation == "create":
            if artifact_digest is None:
                raise ValueError("experiment create requires artifactDigest")
            candidate = await self._visible(user_id, task_id, artifact_digest, mutable=True)
            if ArtifactState(candidate.state) not in {ArtifactState.QUALIFIED, ArtifactState.LEARNED}:
                raise ValueError("experiment candidate must already be qualified or learned")
            control_digest = str(experiment.get("controlArtifactDigest") or "").strip()
            if not control_digest:
                raise ValueError("experiment create requires controlArtifactDigest")
            if control_digest == artifact_digest:
                raise ValueError("experiment control and candidate artifacts must differ")
            control = await self._visible(user_id, task_id, control_digest)
            if ArtifactState(control.state) not in {
                ArtifactState.QUALIFIED,
                ArtifactState.LEARNED,
                ArtifactState.CERTIFIED,
                ArtifactState.CORE,
            }:
                raise ValueError("experiment control artifact is not qualified")
            plan, evidence_id = await self.evolution_engine.create(
                task_id=task_id,
                change_class=ChangeClass(str(experiment.get("changeClass") or "internal")),
                mode=ExperimentMode(str(experiment.get("mode") or "shadow")),
                hypothesis=str(experiment.get("hypothesis") or ""),
                control_artifact_digest=control_digest,
                candidate_artifact_digest=artifact_digest,
                context=ExperimentContext.from_dict(experiment.get("context")),
                min_runs_per_arm=(
                    int(experiment["minRunsPerArm"])
                    if experiment.get("minRunsPerArm") is not None
                    else None
                ),
            )
            return {
                "task": _task(task),
                "experiment": plan.to_api(),
                "experimentEvidenceId": evidence_id,
            }

        experiment_id = str(experiment.get("experimentId") or "").strip()
        if not experiment_id:
            raise ValueError("experimentId is required")
        if operation == "status":
            return {
                "task": _task(task),
                "experiment": await self.evolution_engine.status(
                    task_id=task_id,
                    experiment_id=experiment_id,
                ),
            }
        if operation == "observe":
            observation = await self.evolution_engine.observe(
                task_id=task_id,
                experiment_id=experiment_id,
                arm=ExperimentArmName(str(experiment.get("arm") or "")),
                source_evidence_id=str(experiment.get("sourceEvidenceId") or ""),
            )
            return {
                "task": _task(task),
                "experimentId": experiment_id,
                "observationEvidenceId": observation.evidence_id,
            }
        if operation == "evaluate":
            evaluation = await self.evolution_engine.evaluate(
                task_id=task_id,
                experiment_id=experiment_id,
            )
            return {
                "task": _task(task),
                "experimentId": experiment_id,
                "evaluation": evaluation.to_api(),
                "promoted": False,
            }
        if operation == "cancel":
            evidence_id = await self.evolution_engine.cancel(
                task_id=task_id,
                experiment_id=experiment_id,
                reason=str(experiment.get("reason") or ""),
            )
            return {
                "task": _task(task),
                "experimentId": experiment_id,
                "cancelEvidenceId": evidence_id,
                "cancelled": True,
            }

        plan, evaluation, _rows = await self.evolution_engine.latest_evaluation(
            task_id=task_id,
            experiment_id=experiment_id,
        )
        candidate = await self._visible(
            user_id,
            task_id,
            plan.candidate_artifact_digest,
            mutable=True,
        )
        if artifact_digest is not None and artifact_digest != plan.candidate_artifact_digest:
            raise ValueError("experiment promotion artifactDigest does not match candidate")
        if promotion_target_state is None:
            raise ValueError("experiment promotion requires promotionTargetState")
        target = ArtifactState(str(promotion_target_state))
        current = ArtifactState(candidate.state)
        allowed = {
            ArtifactState.QUALIFIED: {ArtifactState.LEARNED, ArtifactState.CERTIFIED},
            ArtifactState.LEARNED: {ArtifactState.CERTIFIED},
        }
        if target not in allowed.get(current, set()):
            raise ValueError("invalid evolution promotion transition")

        approved = evaluation.decision is PromotionDecision.PROMOTE
        owner_approval_verified = False
        if evaluation.decision is PromotionDecision.OWNER_APPROVAL_REQUIRED:
            if owner_approval_id and self.evolution_approval_verifier is not None:
                verified = self.evolution_approval_verifier(
                    owner_approval_id,
                    {
                        "userId": user_id,
                        "taskId": task_id,
                        "artifactDigest": plan.candidate_artifact_digest,
                        "changeClass": plan.change_class.value,
                        "targetState": target.value,
                        "evaluationEvidenceId": evaluation.evaluation_evidence_id,
                        "experimentId": experiment_id,
                    },
                )
                if inspect.isawaitable(verified):
                    verified = await verified
                owner_approval_verified = verified is True
                approved = owner_approval_verified
        if not approved:
            return {
                "task": _task(task),
                "experimentId": experiment_id,
                "promotionDecision": evaluation.decision.value,
                "evaluationEvidenceId": evaluation.evaluation_evidence_id,
                "promoted": False,
            }
        intent_evidence_id = await self.evolution_engine.record_promotion_intent(
            task_id=task_id,
            experiment_id=experiment_id,
            evaluation=evaluation,
            from_state=current.value,
            target_state=target.value,
            owner_approval_verified=owner_approval_verified,
        )
        updated = await self.store.compare_and_set_artifact_state(
            plan.candidate_artifact_digest,
            expected_state=current.value,
            state=target.value,
        )
        if not updated:
            raise RuntimeError("evolution promotion candidate changed concurrently; reevaluation is required")
        try:
            promotion_evidence_id = await self.evolution_engine.record_promotion(
                task_id=task_id,
                experiment_id=experiment_id,
                evaluation=evaluation,
                intent_evidence_id=intent_evidence_id,
                from_state=current.value,
                target_state=target.value,
                owner_approval_verified=owner_approval_verified,
            )
        except Exception:
            rolled_back = await self.store.compare_and_set_artifact_state(
                plan.candidate_artifact_digest,
                expected_state=target.value,
                state=current.value,
            )
            if not rolled_back:
                raise RuntimeError(
                    "evolution promotion evidence failed and state rollback could not be verified"
                )
            raise
        return {
            "task": _task(task),
            "experimentId": experiment_id,
            "promotionDecision": evaluation.decision.value,
            "evaluationEvidenceId": evaluation.evaluation_evidence_id,
            "promotionEvidenceId": promotion_evidence_id,
            "artifactState": target.value,
            "promoted": True,
        }

    async def reflect(self, *, user_id, task_id, kind, claims, artifact_digest=None, lease_id=None,
                      comparison=None, change_class=None, promotion_target_state=None,
                      owner_approval_id=None, experiment=None):
        task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
        if artifact_digest is not None:
            await self._visible(user_id, task_id, artifact_digest)
        if experiment is not None:
            if not isinstance(experiment, dict):
                raise ValueError("experiment must be an object")
            return await self._reflect_experiment(
                user_id=user_id,
                task_id=task_id,
                task=task,
                artifact_digest=artifact_digest,
                experiment=dict(experiment),
                promotion_target_state=promotion_target_state,
                owner_approval_id=owner_approval_id,
            )
        row = await self.evidence.record(task_id=task_id, kind=kind,
                                         producer_identity=f"capability-os-client:{user_id}",
                                         claims=dict(claims), artifact_digest=artifact_digest, lease_id=lease_id)
        result = {"task": _task(task), "evidenceId": row.evidence_id, "digest": row.digest}
        if comparison is not None:
            control, candidate = comparison.get("control"), comparison.get("candidate")
            if not isinstance(control, dict) or not isinstance(candidate, dict):
                raise ValueError("experiment comparison requires control and candidate arms")
            def arm(p):
                return ExperimentArm(
                    runs=int(p.get("runs") or 0),
                    successes=int(p.get("successes") or 0),
                    regressions=int(p.get("regressions") or 0),
                    safety_events=int(p.get("safetyEvents") or 0),
                    mean_cost=float(p.get("meanCost") or 0.0),
                    model_id=str(p.get("modelId") or ""),
                    reasoning_effort=str(p.get("reasoningEffort") or ""),
                    tool_permission_fingerprint=str(p.get("toolPermissionFingerprint") or ""),
                    resource_budget_fingerprint=str(p.get("resourceBudgetFingerprint") or ""),
                    task_distribution_fingerprint=str(p.get("taskDistributionFingerprint") or ""),
                )
            change = ChangeClass(str(change_class or "internal"))
            parsed_comparison = ExperimentComparison(control=arm(control), candidate=arm(candidate))
            decision = self.evolution.evaluate(change, parsed_comparison)
            evaluation = await self.evidence.record(
                task_id=task_id,
                kind="evolution.evaluation",
                producer_identity="capability-os-control",
                claims={
                    "changeClass": change.value,
                    "decision": decision.value,
                    "artifactDigest": artifact_digest,
                    "control": dict(control),
                    "candidate": dict(candidate),
                },
                artifact_digest=artifact_digest,
            )
            result["promotionDecision"] = decision.value
            result["evaluationEvidenceId"] = evaluation.evidence_id

            if promotion_target_state is not None:
                # Legacy aggregate comparisons are caller-supplied compatibility
                # input. They remain useful for diagnostics, but are not trusted
                # promotion evidence. State changes require the durable experiment
                # lifecycle above, which binds server-produced per-run outcomes.
                result["promoted"] = False
                result["promotionBlocked"] = "durable-experiment-required"
        return result
