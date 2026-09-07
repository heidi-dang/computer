"""Deterministic Capability OS execution VM.

The VM owns transition selection, retry, verification evidence, and compensation
ordering. External actions remain nondeterministic and are supplied by a trusted
executor adapter. The VM never widens a CapabilityLease.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from cptr.services.capability_os.authority import CapabilityLease, permission_covers
from cptr.services.capability_os.compiler import CompiledCapability, DagNode
from cptr.services.capability_os.evidence import EvidenceService


class CapabilityVmError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActionResult:
    output: Any
    verification_passed: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityExecutionResult:
    status: str
    completed_nodes: tuple[str, ...]
    compensated_nodes: tuple[str, ...]
    outputs: dict[str, Any]
    evidence_ids: tuple[str, ...]
    error: str | None = None


class CapabilityActionExecutor(Protocol):
    async def invoke(
        self,
        *,
        action_ref: str,
        version: str,
        inputs: dict[str, Any],
        lease: CapabilityLease,
        timeout_ms: int,
    ) -> ActionResult: ...


class CapabilityVm:
    def __init__(self, *, executor: CapabilityActionExecutor, evidence: EvidenceService, clock_ms) -> None:
        self._executor = executor
        self._evidence = evidence
        self._clock_ms = clock_ms

    def _validate_lease(
        self,
        *,
        task_id: str,
        capability_digest: str,
        lease: CapabilityLease,
        required_permissions=(),
    ) -> None:
        now_ms = int(self._clock_ms())
        if lease.status != "active":
            raise CapabilityVmError("capability execution requires an active lease")
        if lease.expires_at_ms <= now_ms:
            raise CapabilityVmError("capability execution lease has expired")
        if lease.task_id != task_id:
            raise CapabilityVmError("capability execution lease belongs to another task")
        if lease.artifact_digest != capability_digest:
            raise CapabilityVmError("capability execution lease belongs to another artifact")
        if lease.runtime_profile != "cptr-vm":
            raise CapabilityVmError("capability execution lease has the wrong runtime profile")
        for required in required_permissions:
            if not any(permission_covers(allowed, required) for allowed in lease.permissions):
                raise CapabilityVmError("capability execution lease lacks required permission")

    @staticmethod
    def _result_evidence_claims(result: ActionResult) -> dict[str, Any]:
        metadata = dict(result.metadata or {})
        claims: dict[str, Any] = {
            "verificationPassed": result.verification_passed,
            "metadata": metadata,
        }
        if metadata.get("evidenceOutputPolicy") == "digest-only":
            output_digest = str(metadata.get("outputDigest") or "")
            if not output_digest.startswith("sha256:") or len(output_digest) != 71:
                raise CapabilityVmError("digest-only action evidence requires a valid output digest")
            claims["outputDigest"] = output_digest
        else:
            claims["output"] = result.output
        return claims

    async def _invoke_node(
        self,
        *,
        node: DagNode,
        lease: CapabilityLease,
        inputs: dict[str, Any],
    ) -> ActionResult:
        attempts = max(1, node.retry.max_attempts)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = await asyncio.wait_for(
                    self._executor.invoke(
                        action_ref=node.action_ref,
                        version=node.version,
                        inputs=inputs,
                        lease=lease,
                        timeout_ms=node.timeout_ms,
                    ),
                    timeout=node.timeout_ms / 1000,
                )
            except asyncio.TimeoutError:
                last_error = CapabilityVmError(f"node {node.id} exceeded its timeout")
            except Exception as exc:  # executor errors are evidence, not control-plane crashes
                last_error = exc
            else:
                if not isinstance(result, ActionResult):
                    raise CapabilityVmError(f"executor returned an invalid result for node {node.id}")
                return result

            if attempt >= attempts:
                break
            if node.retry.only_if_idempotent and not node.idempotent:
                break
        assert last_error is not None
        raise last_error

    async def execute(
        self,
        *,
        task_id: str,
        capability_digest: str,
        compiled: CompiledCapability,
        lease: CapabilityLease,
        inputs: dict[str, Any],
        approval_id: str | None = None,
    ) -> CapabilityExecutionResult:
        self._validate_lease(
            task_id=task_id,
            capability_digest=capability_digest,
            lease=lease,
            required_permissions=compiled.permissions,
        )
        if approval_id is not None and approval_id != lease.approval_id:
            raise CapabilityVmError("caller approval does not match the server-verified lease approval")
        if compiled.requires_human_approval and not lease.approval_id:
            raise CapabilityVmError("compiled capability requires a server-verified human approval")
        if not isinstance(inputs, dict):
            raise CapabilityVmError("capability inputs must be an object")

        nodes = {node.id: node for node in compiled.spec.nodes}
        completed: list[str] = []
        compensated: list[str] = []
        outputs: dict[str, Any] = {}
        evidence_ids: list[str] = []
        failed_node: str | None = None
        failure_text: str | None = None

        for node_id in compiled.topological_order:
            node = nodes[node_id]
            node_inputs = {
                **inputs,
                "nodeOutputs": dict(outputs),
                **dict(node.input_bindings),
            }
            try:
                result = await self._invoke_node(node=node, lease=lease, inputs=node_inputs)
                if node_id in compiled.spec.verifiers and result.verification_passed is not True:
                    raise CapabilityVmError(f"verifier node {node_id} did not pass")
            except Exception as exc:
                failed_node = node_id
                failure_text = f"{exc.__class__.__name__}: {exc}"
                failure = await self._evidence.record(
                    task_id=task_id,
                    kind="capability.node.failure",
                    producer_identity="capability-vm",
                    claims={
                        "capabilityId": compiled.spec.capability_id,
                        "nodeId": node_id,
                        "actionRef": node.action_ref,
                        "status": "failed",
                        "errorType": exc.__class__.__name__,
                    },
                    artifact_digest=capability_digest,
                    lease_id=lease.lease_id,
                )
                evidence_ids.append(failure.evidence_id)
                break

            outputs[node_id] = result.output
            completed.append(node_id)
            evidence = await self._evidence.record(
                task_id=task_id,
                kind="capability.node",
                producer_identity="capability-vm",
                claims={
                    "capabilityId": compiled.spec.capability_id,
                    "nodeId": node_id,
                    "actionRef": node.action_ref,
                    "status": "pass",
                    **self._result_evidence_claims(result),
                },
                artifact_digest=capability_digest,
                lease_id=lease.lease_id,
            )
            evidence_ids.append(evidence.evidence_id)

        if failed_node is None:
            status = "verified" if compiled.spec.verifiers else "complete"
            return CapabilityExecutionResult(
                status=status,
                completed_nodes=tuple(completed),
                compensated_nodes=(),
                outputs=outputs,
                evidence_ids=tuple(evidence_ids),
            )

        compensation_failed = False
        if compiled.spec.rollback_mode in {"full", "compensating", "partial"}:
            for node_id in reversed(completed):
                node = nodes[node_id]
                if not node.compensation_action_ref:
                    if compiled.spec.rollback_mode in {"full", "compensating"}:
                        compensation_failed = True
                    continue
                try:
                    result = await asyncio.wait_for(
                        self._executor.invoke(
                            action_ref=node.compensation_action_ref,
                            version=node.version,
                            inputs={
                                **inputs,
                                "nodeOutputs": dict(outputs),
                                "compensatingNode": node_id,
                            },
                            lease=lease,
                            timeout_ms=node.timeout_ms,
                        ),
                        timeout=node.timeout_ms / 1000,
                    )
                    if not isinstance(result, ActionResult):
                        raise CapabilityVmError("executor returned invalid compensation result")
                except Exception as exc:
                    compensation_failed = True
                    evidence = await self._evidence.record(
                        task_id=task_id,
                        kind="capability.compensation.failure",
                        producer_identity="capability-vm",
                        claims={
                            "nodeId": node_id,
                            "actionRef": node.compensation_action_ref,
                            "errorType": exc.__class__.__name__,
                        },
                        artifact_digest=capability_digest,
                        lease_id=lease.lease_id,
                    )
                    evidence_ids.append(evidence.evidence_id)
                else:
                    compensated.append(node_id)
                    evidence = await self._evidence.record(
                        task_id=task_id,
                        kind="capability.compensation",
                        producer_identity="capability-vm",
                        claims={
                            "nodeId": node_id,
                            "actionRef": node.compensation_action_ref,
                            "status": "pass",
                            **self._result_evidence_claims(result),
                        },
                        artifact_digest=capability_digest,
                        lease_id=lease.lease_id,
                    )
                    evidence_ids.append(evidence.evidence_id)

        if compiled.spec.rollback_mode == "irreversible":
            status = "failed_irreversible"
        elif compensation_failed:
            status = "compensation_failed"
        elif compensated:
            status = "rolled_back"
        else:
            status = "failed"
        return CapabilityExecutionResult(
            status=status,
            completed_nodes=tuple(completed),
            compensated_nodes=tuple(compensated),
            outputs=outputs,
            evidence_ids=tuple(evidence_ids),
            error=failure_text,
        )
