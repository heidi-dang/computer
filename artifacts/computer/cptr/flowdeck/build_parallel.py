"""Durable CPTR scheduler for isolated Phase 3 Build mutation nodes."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from cptr.flowdeck.authenticated_gateway import (
    SpecialistDispatchRequest,
    dispatch_authenticated_specialist,
)
from cptr.flowdeck.durable import DurableFlowDeck, LifecycleError, RunStatus
from cptr.flowdeck.parallel import ParallelBuildNode, ParallelBuildPlan, ready_parallel_nodes, validate_parallel_build_plan
from cptr.flowdeck.worktrees import (
    BuildWorktree,
    canonical_changed_paths,
    common_base,
    commit_worktree,
    create_worktree,
    integrate_worktree,
    remove_worktree,
    worktree_changed_paths,
)


class ParentBuildCancelled(RuntimeError):
    """Raised when the durable parent is cancelled while a child is running."""


async def _dispatch_with_parent_cancellation(callback, *, store, parent_run_id: str):
    task = asyncio.create_task(callback())
    try:
        while not task.done():
            done, _ = await asyncio.wait((task,), timeout=0.25)
            if done:
                break
            current = await store.get_run(parent_run_id)
            if current is not None and current.status == RunStatus.CANCELLED.value:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise ParentBuildCancelled()
        return await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@dataclass(frozen=True)
class ParallelMutationResult:
    status: str
    nodes: dict[str, dict[str, Any]]
    overlaps: tuple[tuple[str, str], ...] = ()


async def _child_is_authoritative(store: DurableFlowDeck, request_key: str) -> tuple[bool, str | None]:
    child = await store.get_run_by_request_key(request_key)
    if child is None:
        return False, None
    operations = await store.get_run_operations(child.id)
    return (
        child.status == RunStatus.SUCCEEDED.value
        and bool(operations)
        and all(
            operation.status == "SUCCEEDED"
            and isinstance(operation.authoritative_evidence, dict)
            and operation.authoritative_evidence.get("authoritative") is True
            for operation in operations
        ),
        child.id,
    )


def _node_task(task: str, build_request: Any, planning_context: str, focus: str) -> str:
    return (
        f"{task}\n\nBuild brief:\n{build_request.brief.as_dict()}\n"
        f"Architecture:\n{build_request.architecture.as_dict()}\n"
        f"Read-only planning observations:\n{planning_context}\n\n"
        f"Parallel branch focus ({focus}): implement only this branch in the "
        "isolated worktree. Keep changes within the branch scope, use structured "
        "file mutation tools, preserve project conventions, and do not use shell, "
        "Git, browser mutation, network, secrets, package installation, or delegation."
    )


async def run_parallel_build_mutations(
    request: Any,
    *,
    build_request: Any,
    authenticated_request: Any,
    store: DurableFlowDeck,
    planning_outputs: tuple[str, ...] = (),
    steering_checkpoint: Any = None,
    coding_role: str = "backend-coder",
    max_concurrency: int = 2,
) -> dict[str, Any]:
    """Run independent Build branches durably, then integrate them safely."""
    base = await common_base(request.workspace)
    handles: dict[str, BuildWorktree] = {}
    node_specs: list[dict[str, Any]] = []
    planning_context = "\n\n".join(planning_outputs)[-12_000:]
    branch_focus = (
        ("backend", "data model, API, persistence, and server-side behavior"),
        ("frontend", "screens, interaction, responsive layout, and client behavior"),
    )
    try:
        for key, focus in branch_focus:
            handle = await create_worktree(
                canonical_workspace=request.workspace,
                run_id=request.parent_flowdeck_run_id,
                node_key=key,
                common_base=base,
            )
            handles[key] = handle
            node_specs.append(
                {
                    "key": key,
                    "role": coding_role,
                    "mutation": True,
                    "workspace": request.workspace,
                    "worktree": handle.path,
                    "branch": handle.branch,
                    "common_base": base,
                }
            )

        plan = ParallelBuildPlan(
            tuple(
                ParallelBuildNode(
                    key=spec["key"],
                    role=spec["role"],
                    mutation=True,
                    workspace=spec["workspace"],
                    worktree=spec["worktree"],
                    branch=spec["branch"],
                    common_base=spec["common_base"],
                )
                for spec in node_specs
            ),
            max_concurrency=max_concurrency,
        )
        validate_parallel_build_plan(plan)
        await store.create_build_nodes(
            run_id=request.parent_flowdeck_run_id,
            workspace=request.workspace,
            nodes=node_specs,
        )
        await store.record_event(
            request.parent_flowdeck_run_id,
            "BUILD_PARALLEL_DAG_STARTED",
            {
                "common_base": base,
                "max_concurrency": max_concurrency,
                "nodes": [spec["key"] for spec in node_specs],
                "execution_authority": "authenticated-native-cptr",
            },
        )

        state = type("State", (), {"status": {}, "results": {}, "errors": {}})()
        for node in plan.nodes:
            state.status[node.key] = "PENDING"

        async def execute(node: ParallelBuildNode) -> None:
            current = await store.get_run(request.parent_flowdeck_run_id)
            if current is None or current.status == RunStatus.CANCELLED.value:
                return
            owner = f"build-agent:{request.parent_flowdeck_run_id}:{node.key}"
            claimed = await store.claim_build_node(
                run_id=request.parent_flowdeck_run_id,
                node_key=node.key,
                owner=owner,
                fencing_epoch=1,
            )
            execution_started_at = time.time_ns()
            steering = await steering_checkpoint() if steering_checkpoint else []
            instructions = (
                "\n\nHeidi steering instructions:\n"
                + "\n".join(f"- {item}" for item in steering)
                if steering
                else ""
            )
            key = f"{request.request_key}:build:node:{node.key}:attempt:{claimed.attempt}"
            focus = dict(branch_focus)[node.key]
            async def dispatch_child():
                return await dispatch_authenticated_specialist(
                    authenticated_request,
                    SpecialistDispatchRequest(
                        role=node.role,
                        request_key=key,
                        task=_node_task(request.task, build_request, planning_context, focus)
                        + instructions,
                        workspace=request.workspace,
                        execution_workspace=node.worktree,
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

            try:
                await _dispatch_with_parent_cancellation(
                    dispatch_child,
                    store=store,
                    parent_run_id=request.parent_flowdeck_run_id,
                )
                child_ok, child_id = await _child_is_authoritative(store, key)
                if not child_ok:
                    outcome = "failed"
                    await store.finish_build_node(
                        run_id=request.parent_flowdeck_run_id,
                        node_key=node.key,
                        attempt=claimed.attempt,
                        status="FAILED",
                        owner=owner,
                        fencing_epoch=1,
                        evidence={
                            "source": "verifier",
                            "authoritative": True,
                            "observation": "verifier_check",
                            "observed_outcome": outcome,
                            "attempt_id": f"build-node:{request.parent_flowdeck_run_id}:{node.key}:{claimed.attempt}",
                            "child_run_id": child_id,
                            "specialist_claim": None,
                        },
                    )
                    requeued = await store.retry_build_node(
                        run_id=request.parent_flowdeck_run_id,
                        node_key=node.key,
                        max_retries=1,
                    )
                    state.status[node.key] = "PENDING" if requeued else "FAILED"
                    state.errors[node.key] = "child evidence was not authoritative"
                    return

                # A mutation branch must pass an independent structured CPTR
                # check while it is still isolated. The canonical workspace is
                # therefore never used to verify code that has not yet been
                # integrated.
                verification_check = "tests" if node.key == "backend" else "build"
                verification_key = (
                    f"{request.request_key}:build:node:{node.key}:"
                    f"verify:attempt:{claimed.attempt}"
                )
                await dispatch_authenticated_specialist(
                    authenticated_request,
                    SpecialistDispatchRequest(
                        role="tester",
                        request_key=verification_key,
                        task=(
                            f"Verify the isolated Build branch {node.key} with "
                            f"the required {verification_check} check."
                        ),
                        workspace=request.workspace,
                        execution_workspace=node.worktree,
                        model=request.model,
                        connection=request.connection,
                        parent_chat_id=request.parent_chat_id,
                        parent_message_id=request.parent_message_id,
                        parent_flowdeck_run_id=request.parent_flowdeck_run_id,
                        check=verification_check,
                        trusted_repository=True,
                        repository_identity=(
                            f"authenticated-workspace:{request.workspace}"
                        ),
                    ),
                    store=store,
                )
                verification_ok, verification_child_id = (
                    await _child_is_authoritative(store, verification_key)
                )
                if not verification_ok:
                    verification_failed_at = time.time_ns()
                    await store.finish_build_node(
                        run_id=request.parent_flowdeck_run_id,
                        node_key=node.key,
                        attempt=claimed.attempt,
                        status="FAILED",
                        owner=owner,
                        fencing_epoch=1,
                        evidence={
                            "source": "verifier",
                            "authoritative": True,
                            "observation": "verifier_check",
                            "observed_outcome": "failed",
                            "attempt_id": (
                                f"build-node:{request.parent_flowdeck_run_id}:"
                                f"{node.key}:{claimed.attempt}"
                            ),
                            "child_run_id": child_id,
                            "verification_child_run_id": verification_child_id,
                            "verification_check": verification_check,
                            "verification_failed_at": verification_failed_at,
                            "specialist_claim": None,
                        },
                    )
                    requeued = await store.retry_build_node(
                        run_id=request.parent_flowdeck_run_id,
                        node_key=node.key,
                        max_retries=1,
                    )
                    state.status[node.key] = "PENDING" if requeued else "FAILED"
                    state.errors[node.key] = (
                        f"isolated {verification_check} verification failed"
                    )
                    return

                changed = await worktree_changed_paths(handles[node.key])
                commit_hash = await commit_worktree(
                    handles[node.key], f"CPTR Build node {node.key}"
                )
                await store.finish_build_node(
                    run_id=request.parent_flowdeck_run_id,
                    node_key=node.key,
                    attempt=claimed.attempt,
                    status="SUCCEEDED",
                    owner=owner,
                    fencing_epoch=1,
                    evidence={
                        "source": "verifier",
                        "authoritative": True,
                        "observation": "verifier_check",
                        "observed_outcome": "succeeded",
                        "attempt_id": f"build-node:{request.parent_flowdeck_run_id}:{node.key}:{claimed.attempt}",
                        "child_run_id": child_id,
                        "verification_child_run_id": verification_child_id,
                        "verification_check": verification_check,
                        "changed_paths": list(changed),
                        "commit_hash": commit_hash,
                        "execution_started_at": execution_started_at,
                        "execution_finished_at": time.time_ns(),
                        "specialist_claim": None,
                    },
                )
                state.status[node.key] = "SUCCEEDED"
                state.results[node.key] = {
                    "child_run_id": child_id,
                    "changed_paths": changed,
                    "commit_hash": commit_hash,
                    "execution_started_at": execution_started_at,
                    "execution_finished_at": time.time_ns(),
                }
            except ParentBuildCancelled:
                state.status[node.key] = "CANCELLED"
                return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                state.status[node.key] = "FAILED"
                state.errors[node.key] = str(exc)[:500]
                try:
                    await store.finish_build_node(
                        run_id=request.parent_flowdeck_run_id,
                        node_key=node.key,
                        attempt=claimed.attempt,
                        status="FAILED",
                        owner=owner,
                        fencing_epoch=1,
                        evidence={
                            "source": "verifier",
                            "authoritative": True,
                            "observation": "verifier_check",
                            "observed_outcome": "failed",
                            "attempt_id": f"build-node:{request.parent_flowdeck_run_id}:{node.key}:{claimed.attempt}",
                            "error": str(exc)[:500],
                            "specialist_claim": None,
                        },
                    )
                except LifecycleError:
                    pass
                else:
                    requeued = await store.retry_build_node(
                        run_id=request.parent_flowdeck_run_id,
                        node_key=node.key,
                        max_retries=1,
                    )
                    if requeued:
                        state.status[node.key] = "PENDING"

        ready = ready_parallel_nodes(plan, state)
        while ready:
            await asyncio.gather(*(execute(node) for node in ready))
            ready = ready_parallel_nodes(plan, state)
        if any(value != "SUCCEEDED" for value in state.status.values()):
            return {
                "status": "cancelled"
                if (await store.get_run(request.parent_flowdeck_run_id)).status == RunStatus.CANCELLED.value
                else "manual_review_required",
                "nodes": state.results,
                "errors": state.errors,
            }

        changed_by_node = {
            key: tuple(value["changed_paths"])
            for key, value in state.results.items()
        }
        execution_overlaps = []
        execution_intervals = {
            key: (
                int(value["execution_started_at"]),
                int(value["execution_finished_at"]),
            )
            for key, value in state.results.items()
        }
        interval_keys = sorted(execution_intervals)
        for index, left in enumerate(interval_keys):
            for right in interval_keys[index + 1 :]:
                overlap_start = max(
                    execution_intervals[left][0], execution_intervals[right][0]
                )
                overlap_end = min(
                    execution_intervals[left][1], execution_intervals[right][1]
                )
                if overlap_start < overlap_end:
                    execution_overlaps.append(
                        {
                            "nodes": (left, right),
                            "overlap_start": overlap_start,
                            "overlap_end": overlap_end,
                        }
                    )
        if execution_overlaps:
            await store.record_event(
                request.parent_flowdeck_run_id,
                "BUILD_EXECUTION_OVERLAP_DETECTED",
                {"intervals": execution_overlaps},
            )

        overlaps = []
        keys = sorted(changed_by_node)
        for index, left in enumerate(keys):
            for right in keys[index + 1 :]:
                if set(changed_by_node[left]) & set(changed_by_node[right]):
                    overlaps.append((left, right))
        if overlaps:
            await store.record_event(
                request.parent_flowdeck_run_id,
                "BUILD_ACTUAL_OVERLAP_DETECTED",
                {"overlaps": overlaps},
            )

        if await canonical_changed_paths(request.workspace, base):
            await store.record_event(
                request.parent_flowdeck_run_id,
                "BUILD_INTEGRATION_BLOCKED",
                {"reason": "canonical workspace changed during child execution"},
            )
            return {"status": "manual_review_required", "nodes": state.results, "overlaps": overlaps}

        for key in keys:
            result = await integrate_worktree(
                request.workspace,
                handles[key],
                state.results[key]["commit_hash"],
            )
            node_status = "SUCCEEDED" if result["status"] == "succeeded" else "CONFLICT"
            await store.mark_build_node_integration(
                run_id=request.parent_flowdeck_run_id,
                node_key=key,
                status=node_status,
                changed_paths=changed_by_node[key],
                commit_hash=state.results[key]["commit_hash"],
                error=result.get("error"),
            )
            if node_status == "CONFLICT":
                return {
                    "status": "manual_review_required",
                    "nodes": state.results,
                    "overlaps": overlaps,
                }
        await store.record_event(
            request.parent_flowdeck_run_id,
            "BUILD_PARALLEL_INTEGRATION_COMPLETED",
            {"nodes": keys, "overlaps": overlaps, "common_base": base},
        )
        return {
            "status": "succeeded",
            "nodes": state.results,
            "overlaps": overlaps,
            "execution_overlaps": execution_overlaps,
        }
    finally:
        for handle in handles.values():
            try:
                await remove_worktree(handle)
            except (OSError, RuntimeError):
                await store.record_event(
                    request.parent_flowdeck_run_id,
                    "BUILD_WORKTREE_ORPHANED",
                    {"node_key": handle.node_key, "path": handle.path},
                )

