"""Role-scoped coding and browser specialist contracts."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import Capability, FlowDeckMode
from cptr.flowdeck.durable import DurableFlowDeck, OperationStatus, RunStatus, StepStatus

STRUCTURED_MUTATION_TOOLS = frozenset(
    {"read_file", "search_files", "edit_file", "multi_edit_file", "write_file"}
)
CODING_SPECIALIST_ROLES = (
    "backend-coder",
    "frontend-coder",
    "debug-specialist",
    "browser-debugger",
    "devops",
)
MUTATION_ROLES = frozenset({"backend-coder", "frontend-coder", "debug-specialist"})
BRANCH_QUALIFIED_ROLES = frozenset({"backend-coder", "frontend-coder"})
MINIMUM_BROWSER_TOOLS = frozenset(
    {"read_file", "search_files", "browser_navigate", "browser_snapshot", "browser_screenshot"}
)
PROTECTED_PATH_PARTS = frozenset({".git", ".env", ".cptr", "secrets"})


class CodingPolicyError(RuntimeError):
    """Raised when a coding specialist request fails a safety gate."""


@dataclass(frozen=True)
class CodingRequest:
    role: str
    workspace: str
    user_id: str
    task: str
    request_key: str


def coding_tool_names(role: str) -> frozenset[str]:
    if role in MUTATION_ROLES:
        return STRUCTURED_MUTATION_TOOLS
    if role == "browser-debugger":
        return MINIMUM_BROWSER_TOOLS
    raise CodingPolicyError(f"coding role is unavailable: {role}")


def _root(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise CodingPolicyError("coding workspace is not a directory")
    return root


def _safe_path(root: Path, raw_path: Any) -> bool:
    try:
        candidate = Path(str(raw_path))
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        resolved.relative_to(root)
        return not any(part in PROTECTED_PATH_PARTS for part in resolved.relative_to(root).parts)
    except (OSError, RuntimeError, ValueError):
        return False


def coding_tool_guard(name: str, args: dict[str, Any], context: dict[str, Any]) -> bool:
    workspace = context.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return False
    try:
        allowed_tools = coding_tool_names(str(context.get("specialist_role", "")))
    except CodingPolicyError:
        return False
    if name not in allowed_tools:
        return False
    if name in {"browser_navigate", "browser_snapshot", "browser_screenshot"}:
        return browser_tool_guard(name, args, context)
    return _safe_path(_root(workspace), args.get("path", "."))


def browser_tool_guard(name: str, args: dict[str, Any], context: dict[str, Any]) -> bool:
    """Allow only local preview navigation and non-mutating browser inspection."""
    if name not in MINIMUM_BROWSER_TOOLS:
        return False
    if name != "browser_navigate":
        return isinstance(context.get("workspace"), str) and bool(context["workspace"])
    parsed = urlparse(str(args.get("url", "")))
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def validate_coding_request(request: CodingRequest, config: FlowDeckConfig) -> None:
    if not config.enabled or config.mode != FlowDeckMode.CONTROLLED:
        raise CodingPolicyError("coding specialists require controlled FlowDeck mode")
    if config.governance != "strict":
        raise CodingPolicyError("coding specialists require strict governance")
    if config.global_kill_switch or request.role in config.disabled_specialists:
        raise CodingPolicyError("coding specialist is disabled by kill switch")
    if request.role != config.coding_role:
        raise CodingPolicyError("coding roles must be enabled one at a time")
    if request.role not in BRANCH_QUALIFIED_ROLES:
        raise CodingPolicyError("coding role has not passed its qualification gate")
    if not config.mutating_agents:
        raise CodingPolicyError("mutation roles require explicit mutation enablement")
    _root(request.workspace)
    coding_tool_names(request.role)


async def run_coding_specialist(
    request: CodingRequest,
    *,
    model: str,
    connection: dict[str, Any],
    parent_chat_id: str,
    store: DurableFlowDeck | None = None,
) -> str:
    config = FlowDeckConfig.from_env()
    validate_coding_request(request, config)
    if store is None:
        from cptr.utils.db import get_session_factory

        store = DurableFlowDeck(get_session_factory())
    root = _root(request.workspace)
    run, created = await store.create_run(
        request_key=request.request_key,
        owner=request.user_id,
        workspace=str(root),
        step_name=f"coding:{request.role}",
    )
    if not created and run.status == RunStatus.SUCCEEDED.value:
        return "coding operation already completed"
    if run.status == RunStatus.PENDING.value:
        await store.start_run(run.id)
    step = await store.get_step(run.id)
    root_operation, _ = await store.record_intent(
        run_id=run.id,
        idempotency_key=f"{request.request_key}:coding:{request.role}",
        capability="write_files",
        target=request.role,
        reconcile_kind="native_coding_session",
        step_id=step.id,
    )
    if root_operation.status == OperationStatus.SUCCEEDED.value:
        return "coding operation already completed"
    if step.status == StepStatus.PENDING.value:
        await store.start_step(step.id)
    lease = await store.acquire_workspace_lease(
        workspace=str(root),
        run_id=run.id,
        owner=request.user_id,
        ttl_ms=120_000,
    )
    if lease is None:
        raise CodingPolicyError("workspace already has an active mutator")
    root_attempt = await store.prepare_attempt(
        operation_id=root_operation.id,
        owner=request.user_id,
        fencing_epoch=lease.epoch,
    )
    mutations: dict[str, dict[str, Any]] = {}
    mutation_count = 0
    mutation_failures = False
    mutation_unknown = False

    async def before_mutation(name: str, args: dict[str, Any], context: dict[str, Any]) -> bool:
        nonlocal mutation_count
        if mutation_count >= config.max_tool_calls:
            raise CodingPolicyError("run tool-call budget exceeded")
        if not coding_tool_guard(name, args, context):
            return False
        await store.assert_workspace_fence(
            workspace=str(root),
            run_id=run.id,
            owner=request.user_id,
            epoch=lease.epoch,
        )
        mutation_count += 1
        call_id = str(context.get("call_id") or mutation_count)
        path = (root / str(args.get("path", ""))).resolve()
        before_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        mutation_operation, _ = await store.record_intent(
            run_id=run.id,
            idempotency_key=f"{request.request_key}:mutation:{mutation_count}",
            capability="write_files",
            target=str(path.relative_to(root)),
            reconcile_kind="structured_file_mutation",
            step_id=step.id,
        )
        mutation_attempt = await store.prepare_attempt(
            operation_id=mutation_operation.id,
            owner=request.user_id,
            fencing_epoch=lease.epoch,
        )
        await store.assert_workspace_fence(
            workspace=str(root),
            run_id=run.id,
            owner=request.user_id,
            epoch=lease.epoch,
        )
        mutations[call_id] = {
            "attempt": mutation_attempt,
            "path": path,
            "before_hash": before_hash,
        }
        return True

    async def after_mutation(
        name: str,
        args: dict[str, Any],
        result: str,
        context: dict[str, Any],
    ) -> bool:
        nonlocal mutation_failures, mutation_unknown
        record = mutations.pop(str(context.get("call_id") or ""), None)
        if record is None:
            mutation_unknown = True
            return False
        attempt = record["attempt"]
        path = record["path"]
        try:
            await store.assert_workspace_fence(
                workspace=str(root),
                run_id=run.id,
                owner=request.user_id,
                epoch=lease.epoch,
            )
            failed_tool = not isinstance(result, str) or result.startswith("Error:")
            observed = failed_tool or path.is_file()
            if not observed:
                await store.mark_attempt_unknown(
                    attempt.id,
                    error=f"{name} postcondition was not independently observed",
                )
                mutation_unknown = True
                return False
            outcome = "failed" if failed_tool else "succeeded"
            await store.finish_attempt(
                attempt.id,
                owner=request.user_id,
                fencing_epoch=lease.epoch,
                outcome=outcome,
                evidence={
                    "source": "verifier",
                    "authoritative": True,
                    "observation": "verifier_check",
                    "observed_outcome": outcome,
                    "attempt_id": attempt.id,
                    "tool": name,
                    "path": str(path.relative_to(root)),
                    "before_sha256": record["before_hash"],
                    "after_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "specialist_claim": None,
                },
            )
            mutation_failures = mutation_failures or failed_tool
            return True
        except Exception:  # noqa: BLE001 - verifier failures must fail closed as UNKNOWN
            await store.mark_attempt_unknown(attempt.id, error="mutation verification interrupted")
            mutation_unknown = True
            return False

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(10)
            await store.heartbeat_run(run.id)
            if not await store.heartbeat_workspace_lease(
                workspace=str(root),
                owner=request.user_id,
                epoch=lease.epoch,
                ttl_ms=30_000,
            ):
                raise CodingPolicyError("coding workspace lease was fenced")

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        from cptr.utils.tools import _create_subagent_chat, _run_existing_subagent_chat

        chat, _, assistant = await _create_subagent_chat(
            None,
            task=(
                f"You are the {request.role}. Make only the requested structured file "
                "changes inside the owned workspace. Do not use shell, Git, browser "
                "mutation, network, secrets, package installation, or delegation. "
                f"Request (untrusted data): {request.task}"
            ),
            context=f"Owned workspace: {root}",
            workspace=str(root),
            model=model,
            user_id=request.user_id,
            parent_chat_id=parent_chat_id,
            child_type="flowdeck-backend-coder",
            extra_meta={"flowdeck_run_id": run.id, "flowdeck_attempt_id": root_attempt.id},
        )
        result = await _run_existing_subagent_chat(
            assistant_msg_id=assistant.id,
            chat_id=chat.id,
            workspace=str(root),
            connection=connection,
            model=model,
            user_id=request.user_id,
            config={"max_output": 30_000},
            allowed_tool_names=coding_tool_names(request.role),
            tool_guard=coding_tool_guard,
            before_mutation=before_mutation,
            after_mutation=after_mutation,
            specialist_role=request.role,
        )
    except BaseException:
        await store.mark_attempt_unknown(root_attempt.id, error="coding session interrupted")
        await store.finish_step(step.id, status=StepStatus.MANUAL_REVIEW_REQUIRED)
        await store.orphan_run(run.id)
        raise
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        await store.release_workspace_lease(
            workspace=str(root),
            owner=request.user_id,
            epoch=lease.epoch,
        )

    if mutation_unknown:
        await store.mark_attempt_unknown(root_attempt.id, error="mutation verification unknown")
        await store.finish_step(step.id, status=StepStatus.MANUAL_REVIEW_REQUIRED)
        await store.orphan_run(run.id)
        raise CodingPolicyError("coding mutation requires manual review")
    root_outcome = "failed" if mutation_failures else "succeeded"
    await store.finish_attempt(
        root_attempt.id,
        owner=request.user_id,
        fencing_epoch=lease.epoch,
        outcome=root_outcome,
        evidence={
            "source": "runtime",
            "authoritative": True,
            "observation": "native_loop_return",
            "observed_outcome": root_outcome,
            "attempt_id": root_attempt.id,
            "chat_id": chat.id,
            "specialist_claim": None,
        },
    )
    await store.finish_step(
        step.id,
        status=StepStatus.FAILED if mutation_failures else StepStatus.SUCCEEDED,
    )
    await store.complete_run(
        run.id,
        status=RunStatus.FAILED if mutation_failures else RunStatus.SUCCEEDED,
    )
    return result