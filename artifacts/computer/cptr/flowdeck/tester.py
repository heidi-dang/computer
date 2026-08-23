"""Controlled, structured repository checks for FlowDeck."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cptr.flowdeck.budgets import RunBudget
from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import Capability, FlowDeckMode
from cptr.flowdeck.durable import (
    DurableFlowDeck,
    OperationStatus,
    RunStatus,
    StepStatus,
)
from cptr.flowdeck.evidence import validate_terminal_evidence

TEST_CHECKS = frozenset({"tests", "build", "typecheck", "lint"})
_TESTER_OWNER = "flowdeck-tester"
_MAX_TIMEOUT_SECONDS = 300
_MAX_OUTPUT_BYTES = 128 * 1024


class TesterPolicyError(RuntimeError):
    """Raised when a tester request is not explicitly safe to run."""


@dataclass(frozen=True)
class TesterRequest:
    request_key: str
    workspace: str
    user_id: str
    check: str
    trusted_repository: bool
    repository_identity: str
    timeout_seconds: float = 120


def _workspace_root(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise TesterPolicyError("tester workspace is not a directory")
    return root


def validate_tester_request(request: TesterRequest, config: FlowDeckConfig) -> Path:
    if not config.enabled or config.mode != FlowDeckMode.CONTROLLED:
        raise TesterPolicyError("tester requires enabled controlled FlowDeck mode")
    if config.governance != "strict":
        raise TesterPolicyError("tester requires strict governance")
    if not request.trusted_repository or not request.repository_identity.strip():
        raise TesterPolicyError("tester requires explicit trusted repository identity")
    if request.check not in TEST_CHECKS:
        raise TesterPolicyError("tester check is not in the structured allowlist")
    if request.timeout_seconds <= 0 or request.timeout_seconds > _MAX_TIMEOUT_SECONDS:
        raise TesterPolicyError("tester timeout is outside the safe bound")
    return _workspace_root(request.workspace)


def _command_for(check: str) -> tuple[str, ...]:
    if check == "tests":
        return (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py")
    if check == "build":
        return ("npm", "--prefix", "cptr/frontend", "run", "build")
    if check == "typecheck":
        return ("npm", "--prefix", "cptr/frontend", "run", "check")
    if check == "lint":
        return ("ruff", "check", "cptr", "tests")
    raise TesterPolicyError("unknown structured tester check")


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    return await stream.read(limit + 1)


async def _run_check(
    check: str,
    *,
    root: Path,
    timeout_seconds: float,
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *_command_for(check),
        cwd=str(root),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "HOME": str(root)},
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.gather(
                _read_bounded(process.stdout, _MAX_OUTPUT_BYTES),
                _read_bounded(process.stderr, _MAX_OUTPUT_BYTES),
            ),
            timeout=timeout_seconds,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if len(stdout) > _MAX_OUTPUT_BYTES or len(stderr) > _MAX_OUTPUT_BYTES:
        process.kill()
        await process.wait()
        raise TesterPolicyError("tester output exceeded configured bound")
    await process.wait()
    return process.returncode, stdout, stderr


async def run_tester(
    request: TesterRequest,
    *,
    store: DurableFlowDeck | None = None,
) -> dict[str, Any]:
    config = FlowDeckConfig.from_env()
    root = validate_tester_request(request, config)
    if store is None:
        from cptr.utils.db import get_session_factory

        store = DurableFlowDeck(get_session_factory())

    run, created = await store.create_run(
        request_key=request.request_key,
        owner=request.user_id,
        workspace=str(root),
        step_name=f"tester:{request.check}",
    )
    if not created and run.status == RunStatus.SUCCEEDED.value:
        return {"status": "succeeded", "reused": True}
    if run.status == RunStatus.PENDING.value:
        await store.start_run(run.id)
    step = await store.get_step(run.id)
    operation, _ = await store.record_intent(
        run_id=run.id,
        idempotency_key=f"{request.request_key}:tester:{request.check}",
        capability=Capability.EXECUTE_COMMAND.value,
        target=request.check,
        reconcile_kind="structured_tester_exit",
        step_id=step.id,
    )
    if operation.status == OperationStatus.SUCCEEDED.value:
        return {"status": "succeeded", "reused": True}
    if step.status == StepStatus.PENDING.value:
        await store.start_step(step.id)
    lease = await store.acquire_workspace_lease(
        workspace=str(root),
        run_id=run.id,
        owner=_TESTER_OWNER,
        ttl_ms=int(request.timeout_seconds * 1000) + 30_000,
    )
    if lease is None:
        raise TesterPolicyError("workspace already has an active mutator")
    attempt = await store.prepare_attempt(
        operation_id=operation.id,
        owner=_TESTER_OWNER,
        fencing_epoch=lease.epoch,
    )
    budget = RunBudget(
        max_steps=config.max_steps,
        max_attempts=config.max_attempts,
        max_delegations=config.max_specialists,
        max_tool_calls=config.max_tool_calls,
        max_model_turns=config.max_model_turns,
        max_wall_seconds=min(config.max_wall_seconds, int(request.timeout_seconds)),
    )
    budget.consume_step()
    budget.consume_attempt()
    started = time.monotonic()

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(10)
            await store.heartbeat_run(run.id)
            if not await store.heartbeat_workspace_lease(
                workspace=str(root),
                owner=_TESTER_OWNER,
                epoch=lease.epoch,
                ttl_ms=30_000,
            ):
                raise TesterPolicyError("tester workspace lease was fenced")

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        exit_code, stdout, stderr = await _run_check(
            request.check,
            root=root,
            timeout_seconds=request.timeout_seconds,
        )
    except BaseException:
        await store.mark_attempt_unknown(attempt.id, error="tester interrupted")
        await store.finish_step(step.id, status=StepStatus.MANUAL_REVIEW_REQUIRED)
        await store.orphan_run(run.id)
        raise
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        await store.release_workspace_lease(
            workspace=str(root),
            owner=_TESTER_OWNER,
            epoch=lease.epoch,
        )

    outcome = "succeeded" if exit_code == 0 else "failed"
    budget.validate_wall_time(time.monotonic() - started)
    evidence = {
        "source": "runtime",
        "authoritative": True,
        "observation": "verifier_check",
        "observed_outcome": outcome,
        "attempt_id": attempt.id,
        "check": request.check,
        "exit_code": exit_code,
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "repository_identity": request.repository_identity,
        "specialist_claim": None,
    }
    validate_terminal_evidence(evidence, outcome=outcome, attempt_id=attempt.id)
    await store.finish_attempt(
        attempt.id,
        owner=_TESTER_OWNER,
        fencing_epoch=lease.epoch,
        outcome=outcome,
        evidence=evidence,
    )
    await store.finish_step(
        step.id,
        status=StepStatus.SUCCEEDED if exit_code == 0 else StepStatus.FAILED,
    )
    await store.complete_run(
        run.id,
        status=RunStatus.SUCCEEDED if exit_code == 0 else RunStatus.FAILED,
    )
    return {
        "status": outcome,
        "exit_code": exit_code,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
        "evidence": evidence,
    }