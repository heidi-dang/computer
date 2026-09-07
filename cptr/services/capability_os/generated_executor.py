"""Projected execution adapter for qualified content-addressed generated Tools.

Capability VM authority and sandbox runtime authority remain separate. A
``tool://sha256:<digest>`` node must first be covered by the active cptr-vm
lease, then receives a fresh server-policy-bounded runtime.run lease for only
the referenced Tool. The runtime lease is always revoked after one invocation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from cptr.services.capability_os.authority import (
    AuthorityBroker,
    AuthorityDenied,
    CapabilityLease,
    LeaseRequest,
)
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactState,
    CapabilityRequest,
    digest_payload,
)
from cptr.services.capability_os.evidence import EvidenceService
from cptr.services.capability_os.forge import ToolForge, ToolRunResult
from cptr.services.capability_os.policy import AuthorityPolicyProvider
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.services.capability_os.vm import ActionResult


_TOOL_REF_RE = re.compile(r"^tool://(sha256:[0-9a-f]{64})$")
_EXECUTABLE_STATES = frozenset({
    ArtifactState.QUALIFIED.value,
    ArtifactState.LEARNED.value,
    ArtifactState.CERTIFIED.value,
    ArtifactState.CORE.value,
})


@dataclass(frozen=True)
class _LeaseTaskContext:
    user_id: str
    workspace_id: str
    source: str


class ProjectedGeneratedToolExecutor:
    """Route only immutable ``tool://sha256:<digest>`` actions to Tool Forge."""

    def __init__(
        self,
        *,
        base_executor: Any,
        store: SqlCapabilityOsStore,
        authority: AuthorityBroker,
        forge: ToolForge,
        evidence: EvidenceService,
        policy_provider: AuthorityPolicyProvider,
        clock_ms,
    ) -> None:
        self._base = base_executor
        self._store = store
        self._authority = authority
        self._forge = forge
        self._evidence = evidence
        self._policy_provider = policy_provider
        self._clock_ms = clock_ms

    @staticmethod
    def _parse(action_ref: str) -> str | None:
        text = str(action_ref)
        if not text.startswith("tool://"):
            return None
        match = _TOOL_REF_RE.fullmatch(text)
        if match is None:
            raise PermissionError("invalid generated Tool action reference")
        return match.group(1)

    @staticmethod
    def _task_context(lease: CapabilityLease) -> _LeaseTaskContext:
        context = dict(lease.execution_context or {})
        user_id = str(context.get("userId") or "").strip()
        workspace_id = str(context.get("workspaceId") or "").strip()
        source = str(context.get("taskSource") or "").strip()
        if not user_id or not workspace_id or not source:
            raise AuthorityDenied("generated Tool execution requires server-bound task context")
        return _LeaseTaskContext(user_id=user_id, workspace_id=workspace_id, source=source)

    async def invoke(
        self,
        *,
        action_ref: str,
        version: str,
        inputs: dict[str, Any],
        lease: CapabilityLease,
        timeout_ms: int,
    ) -> ActionResult:
        digest = self._parse(action_ref)
        if digest is None:
            return await self._base.invoke(
                action_ref=action_ref,
                version=version,
                inputs=inputs,
                lease=lease,
                timeout_ms=timeout_ms,
            )
        if not isinstance(inputs, dict):
            raise TypeError("generated Tool inputs must be an object")
        if int(timeout_ms) <= 0:
            raise ValueError("generated Tool timeout must be positive")

        required = CapabilityRequest("tool.invoke", f"artifact:{digest}")
        active_vm = await self._authority.require_active(
            lease.lease_id,
            task_id=lease.task_id,
            artifact_digest=lease.artifact_digest,
            required_permissions=(required,),
            runtime_profile="cptr-vm",
        )
        task = self._task_context(active_vm)
        row = await self._store.get_artifact(
            digest,
            user_id=task.user_id,
            include_global=True,
        )
        if row is None or row.kind != ArtifactKind.TOOL.value:
            raise KeyError("generated Tool artifact not found")
        if row.state not in _EXECUTABLE_STATES:
            raise PermissionError("generated Tool is not qualified for execution")
        if str(version) != str(row.version):
            raise PermissionError("generated Tool action version does not match artifact version")

        runtime = dict(row.spec.get("runtime") or {})
        runtime_profile = str(runtime.get("class") or "").strip()
        resources = dict(row.spec.get("resources") or {})
        if not runtime_profile:
            raise PermissionError("generated Tool runtime profile is missing")
        policy = await self._policy_provider.resolve(
            task=task,
            artifact_digest=digest,
            workload_id=f"tool-run:{row.artifact_id}",
        )
        if policy is None:
            raise AuthorityDenied("no standing authority policy covers generated Tool execution")
        if policy.outbound_network != "deny":
            raise AuthorityDenied("generated Tool execution requires network-denied isolation policy")

        runtime_permission = CapabilityRequest("runtime.run", f"artifact:{digest}")
        requested_lease_ms = min(
            int(policy.max_lease_ms),
            max(100, int(timeout_ms)) + 5_000,
        )
        runtime_lease = await self._authority.issue(
            LeaseRequest(
                task_id=active_vm.task_id,
                workload_id=f"tool-run:{row.artifact_id}",
                artifact_digest=digest,
                permissions=(runtime_permission,),
                runtime_profile=runtime_profile,
                requested_lease_ms=requested_lease_ms,
                parent_lease_id=active_vm.lease_id,
                resource_limits=resources,
                execution_context=dict(active_vm.execution_context),
            ),
            policy=policy,
        )
        try:
            try:
                run = await self._forge.run(
                    digest,
                    inputs=dict(inputs),
                    lease=runtime_lease,
                    timeout_ms=int(timeout_ms),
                )
                if not isinstance(run, ToolRunResult):
                    raise TypeError("Tool Forge returned an invalid generated Tool result")
                output_digest = digest_payload(run.output)
            except Exception as exc:
                await self._evidence.record(
                    task_id=active_vm.task_id,
                    kind="tool.run.failure",
                    producer_identity="capability-os-control",
                    claims={"toolId": row.artifact_id, "errorType": exc.__class__.__name__},
                    artifact_digest=digest,
                    lease_id=runtime_lease.lease_id,
                )
                raise

            evidence = await self._evidence.record(
                task_id=active_vm.task_id,
                kind="tool.run",
                producer_identity="capability-os-control",
                claims={
                    "toolId": row.artifact_id,
                    "sourceDigest": run.source_digest,
                    "executionArtifactDigest": run.execution_artifact_digest,
                    "runtimeClass": run.runtime_class,
                    "attestation": run.attestation,
                    "outputDigest": output_digest,
                },
                run_id=run.run_id,
                artifact_digest=digest,
                lease_id=runtime_lease.lease_id,
            )
            return ActionResult(
                output=run.output,
                verification_passed=True,
                metadata={
                    "toolDigest": digest,
                    "sourceDigest": run.source_digest,
                    "executionArtifactDigest": run.execution_artifact_digest,
                    "runtimeClass": run.runtime_class,
                    "attestation": run.attestation,
                    "toolRunEvidenceId": evidence.evidence_id,
                    "outputDigest": output_digest,
                    "evidenceOutputPolicy": "digest-only",
                },
            )
        finally:
            await self._authority.revoke(runtime_lease.lease_id)
