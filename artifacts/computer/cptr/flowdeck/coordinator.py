"""Controlled Heidi coordination over qualified FlowDeck specialists."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cptr.flowdeck.authenticated_gateway import (
    SpecialistDispatchRequest,
    _auth_user_id,
    dispatch_authenticated_specialist,
    resolve_gateway_workspace,
)
from cptr.flowdeck.budgets import RunBudget
from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import Capability, DelegationRequest, FlowDeckMode
from cptr.flowdeck.delegation import validate_delegation
from cptr.flowdeck.durable import (
    DurableFlowDeck,
    OperationStatus,
    RunStatus,
    StepStatus,
)
from cptr.flowdeck.errors import DelegationPolicyError, UnknownAgentError
from cptr.flowdeck.registry import get_agent


class CoordinatorPolicyError(RuntimeError):
    """Raised when controlled coordination cannot be authorized safely."""


@dataclass(frozen=True)
class CoordinatorRequest:
    request_key: str
    task: str
    workspace: str
    model: str
    connection: dict[str, Any]
    parent_chat_id: str


@dataclass(frozen=True)
class PlannedDelegation:
    specialist_id: str
    objective: str
    capabilities: frozenset[Capability]
    optional: bool = False
    check: str = "tests"


@dataclass(frozen=True)
class CoordinatorResult:
    status: str
    run_id: str
    children: tuple[dict[str, Any], ...]
    outputs: tuple[str, ...]


_HINTS: tuple[tuple[str, str], ...] = (
    ("security", "security-auditor"),
    ("vulnerability", "security-auditor"),
    ("audit", "security-auditor"),
    ("architecture", "architect"),
    ("design", "architect"),
    ("research", "researcher"),
    ("investigate", "researcher"),
    ("map", "mapper"),
    ("directory", "mapper"),
    (" dir", "mapper"),
    ("review", "reviewer"),
    ("debug", "debug-specialist"),
    ("error", "debug-specialist"),
    ("browser", "browser-debugger"),
    ("website", "browser-debugger"),
    ("url", "browser-debugger"),
)
_CHECK_HINTS = ("test", "build", "typecheck", "lint")
_MUTATION_HINTS = ("write", "edit", "modify", "change", "create", "update", "delete")


def classify_coordinator_request(
    task: str,
    *,
    coding_role: str = "backend-coder",
) -> tuple[PlannedDelegation, ...]:
    """Produce a deterministic plan; no model or workspace inspection occurs."""
    lowered = (task or "").casefold()
    selected: list[PlannedDelegation] = []
    # A natural-language request often contains incidental words such as
    # "review", "error", or "browser" while naming one clear objective.
    # Choose one primary specialist in the declared priority order instead of
    # turning those incidental matches into an oversized delegation plan.
    for hint, role in _HINTS:
        if hint in lowered:
            selected.append(
                PlannedDelegation(
                    role,
                    task,
                    get_agent(role).capabilities,
                )
            )
            break
    check = next((item for item in _CHECK_HINTS if item in lowered), None)
    if check and "tester" not in {item.specialist_id for item in selected}:
        selected.append(
            PlannedDelegation(
                "tester", task, frozenset({Capability.EXECUTE_COMMAND}), check=check
            )
        )
    if any(item in lowered for item in _MUTATION_HINTS):
        selected.append(
            PlannedDelegation(
                coding_role,
                task,
                get_agent(coding_role).capabilities,
            )
        )
    return tuple(selected)


def _validate_plan(plan: tuple[PlannedDelegation, ...], config: FlowDeckConfig) -> None:
    if not plan:
        raise CoordinatorPolicyError("request does not map to a qualified specialist")
    if len(plan) > config.max_specialists:
        raise CoordinatorPolicyError("specialist delegation budget exceeded")
    seen: set[str] = set()
    for item in plan:
        if item.specialist_id in seen:
            raise CoordinatorPolicyError("duplicate specialist delegation")
        seen.add(item.specialist_id)
        try:
            agent = get_agent(item.specialist_id)
        except UnknownAgentError as exc:
            raise CoordinatorPolicyError("unknown specialist is denied") from exc
        if not agent.enabled or item.specialist_id in config.disabled_specialists:
            raise CoordinatorPolicyError("disabled specialist is denied")
        try:
            validate_delegation(
                DelegationRequest(
                    "heidi",
                    item.specialist_id,
                    depth=1,
                    requested_capabilities=item.capabilities,
                ),
                config,
            )
        except DelegationPolicyError as exc:
            raise CoordinatorPolicyError(str(exc)) from exc
        if item.specialist_id in {"backend-coder", "frontend-coder"}:
            if not config.mutating_agents or config.coding_role != item.specialist_id:
                raise CoordinatorPolicyError("coding specialist is not currently qualified")


async def run_heidi_coordinator(
    request: CoordinatorRequest,
    *,
    authenticated_request: Any,
    store: DurableFlowDeck | None = None,
) -> CoordinatorResult:
    """Authenticated, controlled coordinator; CPTR still executes every child."""
    config = FlowDeckConfig.from_env()
    if (
        not config.enabled
        or config.mode != FlowDeckMode.CONTROLLED
        or config.governance != "strict"
        or config.global_kill_switch
    ):
        raise CoordinatorPolicyError("controlled Heidi coordination is disabled")
    user_id = _auth_user_id(authenticated_request)
    if store is None:
        from cptr.utils.db import get_session_factory

        store = DurableFlowDeck(get_session_factory())
    root = await resolve_gateway_workspace(
        session_factory=store.session_factory,
        user_id=user_id,
        requested_workspace=request.workspace,
    )
    plan = classify_coordinator_request(request.task, coding_role=config.coding_role)
    _validate_plan(plan, config)
    run, created = await store.create_run(
        request_key=request.request_key,
        owner=user_id,
        workspace=root,
        step_name="heidi-coordinator",
    )
    if not created and run.status == RunStatus.SUCCEEDED.value:
        return CoordinatorResult("succeeded", run.id, (), ())
    if run.status == RunStatus.CANCELLED.value:
        return CoordinatorResult("cancelled", run.id, (), ())
    if run.status == RunStatus.PENDING.value:
        await store.start_run(run.id)
    parent_step = await store.get_step(run.id)
    if parent_step.status == StepStatus.PENDING.value:
        await store.start_step(parent_step.id)
    budget = RunBudget(
        max_steps=config.max_steps,
        max_attempts=config.max_attempts,
        max_delegations=config.max_specialists,
        max_tool_calls=config.max_tool_calls,
        max_model_turns=config.max_model_turns,
        max_wall_seconds=config.max_wall_seconds,
    )
    budget.consume_step()
    children: list[dict[str, Any]] = []
    outputs: list[str] = []
    for index, item in enumerate(plan):
        current_run = await store.get_run_by_request_key(request.request_key)
        if current_run and current_run.status == RunStatus.CANCELLED.value:
            return CoordinatorResult("cancelled", current_run.id, tuple(children), tuple(outputs))
        budget.consume_step()
        budget.consume_delegation()
        child_key = f"{request.request_key}:child:{index}:{item.specialist_id}"
        child_step = await store.create_child_step(
            run_id=run.id, name=f"delegate:{item.specialist_id}"
        )
        child_operation, _ = await store.record_intent(
            run_id=run.id,
            idempotency_key=child_key,
            capability="delegate_specialist",
            target=item.specialist_id,
            reconcile_kind="coordinator_child",
            step_id=child_step.id,
        )
        if child_operation.status == OperationStatus.SUCCEEDED.value:
            await store.start_step(child_step.id)
            await store.finish_step(child_step.id, status=StepStatus.SUCCEEDED)
            children.append(
                {
                    "specialist": item.specialist_id,
                    "status": "succeeded",
                    "reused": True,
                }
            )
            continue
        await store.start_step(child_step.id)
        budget.consume_attempt()
        attempt = await store.prepare_attempt(
            operation_id=child_operation.id,
            owner=user_id,
            fencing_epoch=0,
        )
        try:
            output = await dispatch_authenticated_specialist(
                authenticated_request,
                SpecialistDispatchRequest(
                    role=item.specialist_id,
                    request_key=child_key,
                    task=item.objective,
                    workspace=root,
                    model=request.model,
                    connection=request.connection,
                    parent_chat_id=request.parent_chat_id,
                    check=item.check,
                    trusted_repository=True,
                    repository_identity=f"authenticated-workspace:{root}",
                ),
                store=store,
            )
            outputs.append(output)
            child_run = await store.get_run_by_request_key(child_key)
            child_operations = await store.get_run_operations(child_run.id) if child_run else []
            child_ok = (
                child_run is not None
                and child_run.status == RunStatus.SUCCEEDED.value
                and bool(child_operations)
                and all(
                    op.status == OperationStatus.SUCCEEDED.value
                    and isinstance(op.authoritative_evidence, dict)
                    and op.authoritative_evidence.get("authoritative") is True
                    for op in child_operations
                )
            )
            if child_ok:
                await store.finish_attempt(
                    attempt.id,
                    owner=user_id,
                    fencing_epoch=0,
                    outcome="succeeded",
                    evidence={
                        "source": "verifier",
                        "authoritative": True,
                        "observation": "verifier_check",
                        "observed_outcome": "succeeded",
                        "attempt_id": attempt.id,
                        "child_run_id": child_run.id,
                        "specialist_claim": None,
                    },
                )
                await store.finish_step(child_step.id, status=StepStatus.SUCCEEDED)
                children.append({"specialist": item.specialist_id, "status": "succeeded"})
            elif child_run and child_run.status == RunStatus.FAILED.value:
                await store.finish_attempt(
                    attempt.id, owner=user_id, fencing_epoch=0, outcome="failed",
                    evidence={
                        "source": "verifier", "authoritative": True,
                        "observation": "verifier_check", "observed_outcome": "failed",
                        "attempt_id": attempt.id, "child_run_id": child_run.id,
                        "specialist_claim": None,
                    },
                )
                await store.finish_step(child_step.id, status=StepStatus.FAILED)
                children.append({"specialist": item.specialist_id, "status": "failed"})
                break
            else:
                await store.mark_attempt_unknown(
                    attempt.id, error="child outcome was not authoritative"
                )
                await store.finish_step(child_step.id, status=StepStatus.MANUAL_REVIEW_REQUIRED)
                children.append(
                    {
                        "specialist": item.specialist_id,
                        "status": "manual_review_required",
                    }
                )
                break
        except BaseException:
            await store.mark_attempt_unknown(attempt.id, error="coordinator child interrupted")
            await store.finish_step(child_step.id, status=StepStatus.MANUAL_REVIEW_REQUIRED)
            await store.orphan_run(run.id)
            raise
    if any(item["status"] == "failed" for item in children):
        status = RunStatus.FAILED
    elif len(children) != len(plan) or any(
        item["status"] != "succeeded" for item in children
    ):
        status = RunStatus.MANUAL_REVIEW_REQUIRED
    else:
        status = RunStatus.SUCCEEDED
    await store.finish_step(
        parent_step.id,
        status=StepStatus.SUCCEEDED if status == RunStatus.SUCCEEDED else (
            StepStatus.FAILED if status == RunStatus.FAILED else StepStatus.MANUAL_REVIEW_REQUIRED
        ),
    )
    await store.complete_run(run.id, status=status)
    return CoordinatorResult(status.value.lower(), run.id, tuple(children), tuple(outputs))