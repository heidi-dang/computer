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
from cptr.services.capability_os.validation import (
    ContractValidationError,
    evaluate_predicates,
    validate_schema,
)


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
        timeout_ms: int | None = None,
    ) -> ActionResult:
        effective_timeout_ms = min(node.timeout_ms, int(timeout_ms or node.timeout_ms))
        if effective_timeout_ms <= 0:
            raise CapabilityVmError(f"node {node.id} has no remaining execution time")
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
                        timeout_ms=effective_timeout_ms,
                    ),
                    timeout=effective_timeout_ms / 1000,
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

        try:
            validate_schema(inputs, compiled.spec.inputs_schema)
            evaluate_predicates(
                compiled.spec.preconditions,
                context={"inputs": inputs, "nodeOutputs": {}},
                label="capability precondition",
            )
        except ContractValidationError as exc:
            raise CapabilityVmError(str(exc)) from exc

        nodes = {node.id: node for node in compiled.spec.nodes}
        dependencies: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        for edge in compiled.spec.edges:
            dependencies[edge.target].add(edge.source)
        completed: list[str] = []
        completed_set: set[str] = set()
        compensated: list[str] = []
        outputs: dict[str, Any] = {}
        evidence_ids: list[str] = []
        failed_node: str | None = None
        failure_text: str | None = None
        pending = set(nodes)
        loop = asyncio.get_running_loop()
        deadline_at = loop.time() + (compiled.spec.deadline_ms / 1000)

        while pending and failed_node is None:
            ready = sorted(
                node_id for node_id in pending
                if dependencies[node_id].issubset(completed_set)
            )
            if not ready:
                raise CapabilityVmError("compiled DAG made no execution progress")
            batch = ready[: compiled.spec.max_parallelism]
            snapshot_outputs = dict(outputs)
            remaining_ms = int((deadline_at - loop.time()) * 1000)
            if remaining_ms <= 0:
                failed_node = batch[0]
                failure_text = "CapabilityVmError: capability deadline exceeded"
                batch_results: list[ActionResult | BaseException] = [
                    CapabilityVmError("capability deadline exceeded")
                ]
                batch = batch[:1]
            else:
                coroutines = []
                for node_id in batch:
                    node = nodes[node_id]
                    node_inputs = {
                        **inputs,
                        "nodeOutputs": snapshot_outputs,
                        **dict(node.input_bindings),
                    }
                    try:
                        evaluate_predicates(
                            node.preconditions,
                            context={"inputs": inputs, "nodeOutputs": snapshot_outputs},
                            label=f"node {node_id} precondition",
                        )
                    except ContractValidationError as exc:
                        async def failed_precondition(error=exc):
                            raise CapabilityVmError(str(error)) from error
                        coroutines.append(failed_precondition())
                    else:
                        coroutines.append(
                            self._invoke_node(
                                node=node,
                                lease=lease,
                                inputs=node_inputs,
                                timeout_ms=remaining_ms,
                            )
                        )
                batch_results = list(await asyncio.gather(*coroutines, return_exceptions=True))

            for node_id, raw_result in zip(batch, batch_results, strict=True):
                node = nodes[node_id]
                result: ActionResult | None = None
                error: BaseException | None = raw_result if isinstance(raw_result, BaseException) else None
                if error is None:
                    result = raw_result
                    try:
                        if node_id in compiled.spec.verifiers and result.verification_passed is not True:
                            raise CapabilityVmError(f"verifier node {node_id} did not pass")
                        validate_schema(result.output, node.output_schema)
                        evaluate_predicates(
                            node.postconditions,
                            context={
                                "inputs": inputs,
                                "nodeOutputs": snapshot_outputs,
                                "result": result.output,
                            },
                            label=f"node {node_id} postcondition",
                        )
                    except (CapabilityVmError, ContractValidationError) as exc:
                        error = exc

                if error is not None:
                    # If the external action returned but its verifier/schema/postcondition
                    # failed, the side effect still happened. Track it as completed so the
                    # declared compensation path can undo it instead of pretending it never ran.
                    if result is not None:
                        outputs[node_id] = result.output
                        completed.append(node_id)
                        completed_set.add(node_id)
                    if failed_node is None:
                        failed_node = node_id
                        failure_text = f"{error.__class__.__name__}: {error}"
                    failure = await self._evidence.record(
                        task_id=task_id,
                        kind="capability.node.failure",
                        producer_identity="capability-vm",
                        claims={
                            "capabilityId": compiled.spec.capability_id,
                            "nodeId": node_id,
                            "actionRef": node.action_ref,
                            "status": "failed",
                            "errorType": error.__class__.__name__,
                        },
                        artifact_digest=capability_digest,
                        lease_id=lease.lease_id,
                    )
                    evidence_ids.append(failure.evidence_id)
                    pending.discard(node_id)
                    continue

                assert result is not None
                outputs[node_id] = result.output
                completed.append(node_id)
                completed_set.add(node_id)
                pending.discard(node_id)
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
