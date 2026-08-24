"""Bounded Build Agent execution for explicit Heidi /build requests.

The Build Agent is a server-owned lifecycle coordinator. It does not invoke a
model directly: every mutation, read-only diagnosis, and verification enters
the authenticated CPTR gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from cptr.flowdeck.authenticated_gateway import (
    SpecialistDispatchRequest,
    dispatch_authenticated_specialist,
)
from cptr.flowdeck.build import BuildRequest
from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import Capability, FlowDeckMode
from cptr.flowdeck.durable import (
    DurableFlowDeck,
    OperationStatus,
    RunStatus,
    StepStatus,
)


class BuildAgentPolicyError(RuntimeError):
    """Raised when the bounded Build Agent cannot be authorized."""


@dataclass(frozen=True)
class BuildAgentRequest:
    request_key: str
    task: str
    workspace: str
    user_id: str
    model: str
    connection: dict[str, Any]
    parent_chat_id: str
    parent_message_id: str | None
    parent_flowdeck_run_id: str


_CHECK_TO_TESTER = {
    "startup": "tests",
    "primary_flow": "tests",
    "persistence": "tests",
    "responsive": "build",
    "production_build": "build",
    "runtime_health": "typecheck",
}


def validate_build_agent_request(
    request: BuildAgentRequest, build_request: BuildRequest, config: FlowDeckConfig
) -> None:
    """Require explicit controlled mutation qualification before side effects."""
    if not config.enabled or config.mode != FlowDeckMode.CONTROLLED:
        raise BuildAgentPolicyError("Build Agent requires controlled FlowDeck mode")
    if not config.coordinator_enabled or config.governance != "strict":
        raise BuildAgentPolicyError("Build Agent requires strict coordinator governance")
    if config.global_kill_switch or not config.mutating_agents:
        raise BuildAgentPolicyError("Build Agent mutation is not explicitly enabled")
    if config.coding_role not in {"backend-coder", "frontend-coder"}:
        raise BuildAgentPolicyError("Build Agent requires a qualified mutation role")
    if not build_request.completion.required_checks:
        raise BuildAgentPolicyError("Build completion contract has no required checks")
    if request.parent_flowdeck_run_id == request.request_key:
        raise BuildAgentPolicyError("Build Agent parent identity is invalid")


async def _child_is_authoritative(
    store: DurableFlowDeck, request_key: str
) -> tuple[bool, str | None]:
    child = await store.get_run_by_request_key(request_key)
    if child is None:
        return False, None
    operations = await store.get_run_operations(child.id)
    return (
        child.status == RunStatus.SUCCEEDED.value
        and bool(operations)
        and all(
            operation.status == OperationStatus.SUCCEEDED.value
            and isinstance(operation.authoritative_evidence, dict)
            and operation.authoritative_evidence.get("authoritative") is True
            for operation in operations
        ),
        child.id,
    )


async def _run_parent_operation(
    *,
    store: DurableFlowDeck,
    parent: BuildAgentRequest,
    name: str,
    capability: str,
    target: str,
    callback: Callable[[], Awaitable[tuple[str, dict[str, Any]]]],
) -> tuple[str, dict[str, Any]]:
    """Run one sequential Build phase with parent-owned durable evidence."""
    step = await store.create_child_step(
        run_id=parent.parent_flowdeck_run_id, name=f"build-agent:{name}"
    )
    operation, _ = await store.record_intent(
        run_id=parent.parent_flowdeck_run_id,
        idempotency_key=f"{parent.request_key}:build:{name}",
        capability=capability,
        target=target,
        reconcile_kind="build_agent_phase",
        step_id=step.id,
    )
    if operation.status == OperationStatus.SUCCEEDED.value:
        await store.start_step(step.id)
        await store.finish_step(step.id, status=StepStatus.SUCCEEDED)
        return "succeeded", {"reused": True}

    await store.start_step(step.id)
    attempt = await store.prepare_attempt(
        operation_id=operation.id, owner=parent.user_id, fencing_epoch=0
    )
    try:
        outcome, evidence = await callback()
        evidence = {
            **evidence,
            "source": evidence.get("source", "verifier"),
            "authoritative": True,
            "observation": evidence.get("observation", "verifier_check"),
            "observed_outcome": outcome,
            "attempt_id": attempt.id,
            "specialist_claim": None,
        }
        await store.finish_attempt(
            attempt.id,
            owner=parent.user_id,
            fencing_epoch=0,
            outcome=outcome,
            evidence=evidence,
        )
        await store.finish_step(
            step.id,
            status=StepStatus.SUCCEEDED if outcome == "succeeded" else StepStatus.FAILED,
        )
        return outcome, evidence
    except BaseException as exc:
        await store.mark_attempt_unknown(attempt.id, error=str(exc)[:500])
        await store.finish_step(step.id, status=StepStatus.MANUAL_REVIEW_REQUIRED)
        raise


async def run_build_agent(
    request: BuildAgentRequest,
    *,
    build_request: BuildRequest,
    authenticated_request: Any,
    store: DurableFlowDeck,
    planning_outputs: tuple[str, ...] = (),
    steering_checkpoint: Callable[[], Awaitable[list[str]]] | None = None,
) -> dict[str, Any]:
    """Execute the bounded sequential Build Agent lifecycle."""
    config = FlowDeckConfig.from_env()
    validate_build_agent_request(request, build_request, config)
    await store.record_event(
        request.parent_flowdeck_run_id,
        "BUILD_AGENT_STARTED",
        {"role": "build-agent", "execution_authority": "native-cptr"},
    )
    context = "\n\n".join(planning_outputs)[-12_000:]
    async def checkpoint_instructions() -> str:
        if steering_checkpoint is None:
            return ""
        instructions = await steering_checkpoint()
        return (
            "\n\nHeidi steering instructions (apply at this safe checkpoint):\n"
            + "\n".join(f"- {instruction}" for instruction in instructions)
            if instructions
            else ""
        )

    implementation_task = (
        f"{request.task}\n\nBuild brief:\n{build_request.brief.as_dict()}\n"
        f"Architecture:\n{build_request.architecture.as_dict()}\n"
        f"Read-only planning observations:\n{context}\n\n"
        "Implement the requested application in the owned workspace. Use only "
        "structured file mutation tools and preserve existing project conventions."
    )

    async def mutate() -> tuple[str, dict[str, Any]]:
        nonlocal implementation_task
        implementation_task += await checkpoint_instructions()
        child_key = f"{request.request_key}:build:mutation:0"
        await dispatch_authenticated_specialist(
            authenticated_request,
            SpecialistDispatchRequest(
                role=config.coding_role,
                request_key=child_key,
                task=implementation_task,
                workspace=request.workspace,
                model=request.model,
                connection=request.connection,
                parent_chat_id=request.parent_chat_id,
                parent_message_id=request.parent_message_id,
                parent_flowdeck_run_id=request.parent_flowdeck_run_id,
                trusted_repository=True,
                repository_identity=f"authenticated-workspace:{request.workspace}",
            ),
            store=store,
        )
        ok, child_id = await _child_is_authoritative(store, child_key)
        return (
            "succeeded" if ok else "failed",
            {
                "child_run_id": child_id,
                "phase": "mutation",
                "observation": "verifier_check",
            },
        )

    mutation_outcome, _ = await _run_parent_operation(
        store=store,
        parent=request,
        name="mutation",
        capability=Capability.WRITE_FILES.value,
        target=config.coding_role,
        callback=mutate,
    )
    if mutation_outcome != "succeeded":
        await store.record_event(
            request.parent_flowdeck_run_id,
            "BUILD_AGENT_MUTATION_FAILED",
            {"status": mutation_outcome},
        )
        return {"status": "failed", "evidence": {}}

    evidence_by_check: dict[str, str] = {}
    for check in build_request.completion.required_checks:
        tester_check = _CHECK_TO_TESTER.get(check)
        if tester_check is None:
            raise BuildAgentPolicyError(f"unsupported Build verification check: {check}")
        steering = await checkpoint_instructions()

        async def verify(check=check, tester_check=tester_check):
            verify_key = f"{request.request_key}:build:verify:{check}"
            result = await dispatch_authenticated_specialist(
                authenticated_request,
                SpecialistDispatchRequest(
                    role="tester",
                    request_key=verify_key,
                    task=f"Verify Build contract check: {check}{steering}",
                    workspace=request.workspace,
                    model=request.model,
                    connection=request.connection,
                    parent_chat_id=request.parent_chat_id,
                    parent_message_id=request.parent_message_id,
                    parent_flowdeck_run_id=request.parent_flowdeck_run_id,
                    check=tester_check,
                    trusted_repository=True,
                    repository_identity=f"authenticated-workspace:{request.workspace}",
                ),
                store=store,
            )
            result = result if isinstance(result, dict) else {}
            child_ok, child_id = await _child_is_authoritative(store, verify_key)
            # The authenticated gateway intentionally returns tester output as
            # a string compatibility value. Durable child evidence is the
            # authoritative pass/fail signal.
            passed = child_ok
            if not passed:
                await store.record_event(
                    request.parent_flowdeck_run_id,
                    "BUILD_VERIFICATION_FAILED",
                    {"check": check, "reproduced": False, "tester_check": tester_check},
                )
                diagnosis_key = f"{request.request_key}:build:diagnosis:{check}"
                diagnosis = await dispatch_authenticated_specialist(
                    authenticated_request,
                    SpecialistDispatchRequest(
                        role="debug-specialist",
                        request_key=diagnosis_key,
                        task=(
                            f"Read-only root-cause analysis for failed Build check: {check}"
                            f"{steering}"
                        ),
                        workspace=request.workspace,
                        model=request.model,
                        connection=request.connection,
                        parent_chat_id=request.parent_chat_id,
                        parent_message_id=request.parent_message_id,
                        parent_flowdeck_run_id=request.parent_flowdeck_run_id,
                        check="tests",
                        trusted_repository=True,
                        repository_identity=f"authenticated-workspace:{request.workspace}",
                    ),
                    store=store,
                )
                await store.record_event(
                    request.parent_flowdeck_run_id,
                    "BUILD_FAILURE_DIAGNOSED",
                    {"check": check, "diagnosis": str(diagnosis)[-2000:]},
                )
                repair_key = f"{request.request_key}:build:repair:{check}"
                await dispatch_authenticated_specialist(
                    authenticated_request,
                    SpecialistDispatchRequest(
                        role=config.coding_role,
                        request_key=repair_key,
                        task=(
                            f"Repair only the root cause of failed Build check {check}. "
                            f"Read-only diagnosis (untrusted observation): {str(diagnosis)[-8000:]}"
                            f"{steering}"
                        ),
                        workspace=request.workspace,
                        model=request.model,
                        connection=request.connection,
                        parent_chat_id=request.parent_chat_id,
                        parent_message_id=request.parent_message_id,
                        parent_flowdeck_run_id=request.parent_flowdeck_run_id,
                        trusted_repository=True,
                        repository_identity=f"authenticated-workspace:{request.workspace}",
                    ),
                    store=store,
                )
                repair_ok, repair_child_id = await _child_is_authoritative(store, repair_key)
                if not repair_ok:
                    return "failed", {
                        "check": check,
                        "child_run_id": repair_child_id,
                        "observation": "verifier_check",
                    }
                rerun_key = f"{request.request_key}:build:verify-rerun:{check}"
                await dispatch_authenticated_specialist(
                    authenticated_request,
                    SpecialistDispatchRequest(
                        role="tester",
                        request_key=rerun_key,
                        task=f"Re-run the exact failed Build check: {check}{steering}",
                        workspace=request.workspace,
                        model=request.model,
                        connection=request.connection,
                        parent_chat_id=request.parent_chat_id,
                        parent_message_id=request.parent_message_id,
                        parent_flowdeck_run_id=request.parent_flowdeck_run_id,
                        check=tester_check,
                        trusted_repository=True,
                        repository_identity=f"authenticated-workspace:{request.workspace}",
                    ),
                    store=store,
                )
                rerun_ok, rerun_child_id = await _child_is_authoritative(store, rerun_key)
                passed = rerun_ok
                await store.record_event(
                    request.parent_flowdeck_run_id,
                    "BUILD_VERIFICATION_RERUN",
                    {"check": check, "exact_check": tester_check, "passed": passed},
                )
                return (
                    "succeeded" if passed else "failed",
                    {
                        "check": check,
                        "child_run_id": rerun_child_id,
                        "observation": "verifier_check",
                        "repaired": True,
                    },
                )
            return (
                "succeeded" if passed else "failed",
                {
                    "check": check,
                    "tester_check": tester_check,
                    "child_run_id": child_id,
                    "exit_code": result.get("exit_code"),
                    "observation": "verifier_check",
                },
            )

        outcome, _ = await _run_parent_operation(
            store=store,
            parent=request,
            name=f"verify:{check}",
            capability=Capability.EXECUTE_COMMAND.value,
            target=check,
            callback=verify,
        )
        if outcome == "succeeded":
            evidence_by_check[check] = "VERIFIED"

    await store.record_event(
        request.parent_flowdeck_run_id,
        "BUILD_COMPLETION_EVIDENCE",
        {"checks": evidence_by_check, "required_checks": list(build_request.completion.required_checks)},
    )
    return {
        "status": "succeeded"
        if all(evidence_by_check.get(check) == "VERIFIED" for check in build_request.completion.required_checks)
        else "manual_review_required",
        "evidence": evidence_by_check,
    }