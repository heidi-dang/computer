"""Read-only owner-scoped observability for the Capability OS operator console."""

from __future__ import annotations

import time

from cptr.services.capability_os.contracts import ArtifactKind, ArtifactState
from cptr.services.capability_os.evidence import EvidenceService
from cptr.services.capability_os.runtime import RuntimeBroker
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.services.capability_os.tasks import CapabilityTaskCoordinator


class CapabilityOsOperatorService:
    def __init__(
        self,
        *,
        store: SqlCapabilityOsStore,
        tasks: CapabilityTaskCoordinator,
        runtime: RuntimeBroker,
        evidence: EvidenceService | None = None,
    ) -> None:
        self.store = store
        self.tasks = tasks
        self.runtime = runtime
        self.evidence = evidence or EvidenceService(
            store=store,
            clock_ms=lambda: int(time.time() * 1000),
        )

    async def list_tasks(self, *, user_id: str, limit: int = 20) -> dict[str, object]:
        tasks = await self.tasks.list_recent(user_id=user_id, limit=limit)
        return {
            "tasks": [
                {
                    "taskId": task.task_id,
                    "workspaceId": task.workspace_id,
                    "source": task.source,
                    "status": task.status,
                    "active": task.active,
                    "executionAllowed": task.execution_allowed,
                    "label": task.label,
                    "updatedAtMs": task.updated_at_ms,
                }
                for task in tasks
            ]
        }

    async def snapshot(
        self,
        *,
        user_id: str,
        task_id: str,
        limit: int = 100,
    ) -> dict[str, object]:
        task = await self.tasks.require_active(user_id=user_id, task_id=task_id)
        bounded_limit = max(1, min(int(limit), 100))
        rows = await self.store.list_artifacts(
            user_id=user_id,
            include_global=True,
            limit=bounded_limit,
        )
        artifacts = [
            row
            for row in rows
            if row.state != ArtifactState.EPHEMERAL.value or row.task_origin == task_id
        ]
        leases = await self.store.list_active_leases(task_id, now_ms=int(time.time() * 1000))
        mounts = await self.store.list_active_mounts(task_id)
        evidence = await self.store.list_evidence(task_id, limit=bounded_limit)
        evidence_chain = await self.evidence.verify_chain(task_id)
        relational = await self.store.operator_snapshot(task_id=task_id)

        artifact_kinds: dict[str, int] = {}
        artifact_states: dict[str, int] = {}
        for artifact in artifacts:
            artifact_kinds[artifact.kind] = artifact_kinds.get(artifact.kind, 0) + 1
            artifact_states[artifact.state] = artifact_states.get(artifact.state, 0) + 1

        evidence_kinds: dict[str, int] = {}
        for item in evidence:
            evidence_kinds[item.kind] = evidence_kinds.get(item.kind, 0) + 1

        def evidence_count(*prefixes: str) -> int:
            return sum(
                count
                for kind, count in evidence_kinds.items()
                if any(kind == prefix or kind.startswith(f"{prefix}.") for prefix in prefixes)
            )

        learned_or_higher = sum(
            int(artifact_states.get(state.value, 0))
            for state in (ArtifactState.LEARNED, ArtifactState.CERTIFIED, ArtifactState.CORE)
        )
        runtime = self.runtime.production_snapshot()
        views = {
            "taskCausality": {
                "runs": int(relational.get("runs") or 0),
                "activeRuns": int(relational.get("activeRuns") or 0),
                "evidenceRecords": len(evidence),
                "observations": int(relational.get("observations") or 0),
                "evidenceChain": evidence_chain,
            },
            "capabilityHealth": {
                "artifacts": len(artifacts),
                "capabilities": int(artifact_kinds.get(ArtifactKind.CAPABILITY.value, 0)),
                "byKind": artifact_kinds,
                "byState": artifact_states,
            },
            "forge": {
                "tools": int(artifact_kinds.get(ArtifactKind.TOOL.value, 0)),
                "builds": evidence_count("tool.build"),
                "runs": evidence_count("tool.run"),
                "failures": sum(
                    count
                    for kind, count in evidence_kinds.items()
                    if kind.startswith("tool.") and kind.endswith(".failure")
                ),
            },
            "skillEvolution": {
                "skills": int(artifact_kinds.get(ArtifactKind.SKILL.value, 0)),
                "evaluations": evidence_count("skill.evaluation"),
                "promotions": evidence_count("skill.promotion"),
            },
            "mcpFabric": {
                "adapters": int(artifact_kinds.get(ArtifactKind.MCP_ADAPTER.value, 0)),
                "activeMounts": len(mounts),
                "events": evidence_count("mcp"),
            },
            "authority": {
                "activeLeases": len(leases),
                "policyDecisions": int(relational.get("policyDecisions") or 0),
            },
            "sandbox": {
                "runtime": runtime,
            },
            "evolution": {
                "experiments": int(relational.get("experiments") or 0),
                "activeExperiments": int(relational.get("activeExperiments") or 0),
                "events": evidence_count("evolution"),
                "promotions": evidence_count("evolution.promotion"),
            },
            "releases": {
                "learnedOrHigher": learned_or_higher,
                "certified": int(artifact_states.get(ArtifactState.CERTIFIED.value, 0)),
                "core": int(artifact_states.get(ArtifactState.CORE.value, 0)),
                "supplyChainBuilds": evidence_count("tool.build"),
            },
        }
        return {
            "task": {
                "taskId": task.task_id,
                "workspaceId": task.workspace_id,
                "source": task.source,
                "status": task.status,
                "active": task.active,
                "executionAllowed": task.execution_allowed,
            },
            "views": views,
            "activeLeases": [
                {
                    "leaseId": row.lease_id,
                    "artifactDigest": row.artifact_digest,
                    "permissions": list(row.permissions or []),
                    "runtimeProfile": row.runtime_profile,
                    "expiresAtMs": int(row.expires_at_ms),
                }
                for row in leases
            ],
            "activeMounts": [
                {
                    "mountId": row.mount_id,
                    "serverId": row.server_id,
                    "projectedTools": list(row.projected_tools or []),
                }
                for row in mounts
            ],
            "artifactStates": artifact_states,
            "evidenceKinds": evidence_kinds,
        }
