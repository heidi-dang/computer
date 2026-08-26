"""Durable executors for approval-gated direct code blocks and SSH operations.

The manager owns child processes by direct-operation ID. It never uses a local
shell: sandbox code is sent on stdin to a configured trusted runner, while SSH
is spawned with a fixed argv profile and an approved remote command.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cptr.models import Workspace
from cptr.services.direct_operations import DirectOperationStore, WorkspaceBusy
from cptr.utils.db import get_db

MAX_OUTPUT_CHARS = 8_000
MAX_CODE_BYTES = 200_000
MAX_REMOTE_COMMAND_BYTES = 100_000
MAX_EXECUTION_SECONDS = 60
_SAFE_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SAFE_ACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SAFE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_SAFE_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


class ExecutorUnavailable(RuntimeError):
    code = "SANDBOX_EXECUTOR_UNAVAILABLE"


class InvalidSshProfile(RuntimeError):
    code = "SSH_PROFILE_UNAVAILABLE"


@dataclass(frozen=True)
class SshProfile:
    profile_id: str
    host: str
    user: str
    port: int
    identity_file: str
    known_hosts_file: str
    actions: dict[str, str]


def _bounded(text: bytes) -> str:
    decoded = text.decode("utf-8", errors="replace")
    return decoded[-MAX_OUTPUT_CHARS:]


def _profile(profile_id: str) -> SshProfile:
    if not _SAFE_PROFILE_ID.fullmatch(profile_id):
        raise InvalidSshProfile("invalid SSH profile ID")
    raw = os.environ.get("CPTR_DIRECT_SSH_PROFILES_JSON", "").strip()
    if not raw:
        raise InvalidSshProfile("no SSH profiles configured")
    try:
        profiles = json.loads(raw)
        item = profiles.get(profile_id) if isinstance(profiles, dict) else None
    except json.JSONDecodeError as exc:
        raise InvalidSshProfile("invalid SSH profile configuration") from exc
    if not isinstance(item, dict):
        raise InvalidSshProfile("SSH profile not found")
    host = str(item.get("host") or "")
    user = str(item.get("user") or "")
    port = item.get("port", 22)
    identity_file = str(item.get("identity_file") or "")
    known_hosts_file = str(item.get("known_hosts_file") or "")
    configured_actions = item.get("actions")
    actions = {
        action_id: command
        for action_id, command in (configured_actions.items() if isinstance(configured_actions, dict) else [])
        if isinstance(action_id, str)
        and _SAFE_ACTION_ID.fullmatch(action_id)
        and isinstance(command, str)
        and command
        and "\x00" not in command
        and len(command.encode("utf-8")) <= MAX_REMOTE_COMMAND_BYTES
    }
    if (
        not _SAFE_HOST.fullmatch(host)
        or not _SAFE_USER.fullmatch(user)
        or not isinstance(port, int)
        or port < 1
        or port > 65535
        or not Path(identity_file).is_file()
        or not Path(known_hosts_file).is_file()
        or not actions
    ):
        raise InvalidSshProfile("SSH profile is incomplete or invalid")
    return SshProfile(profile_id, host, user, port, identity_file, known_hosts_file, actions)


class DirectExecutorManager:
    """Own and cancel external durable-operation processes until quiescent."""

    def __init__(self, store: DirectOperationStore) -> None:
        self.store = store
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._lock = asyncio.Lock()

    async def schedule(self, operation_id: str) -> None:
        async with self._lock:
            task = self._tasks.get(operation_id)
            if task is not None and not task.done():
                return
            self._tasks[operation_id] = asyncio.create_task(self._run(operation_id))

    async def cancel(self, operation_id: str) -> bool:
        async with self._lock:
            process = self._processes.get(operation_id)
            task = self._tasks.get(operation_id)
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            return True
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    async def shutdown(self) -> None:
        """Cancel owned operations and wait until child tasks have released their leases."""
        async with self._lock:
            operation_ids = list(self._tasks)
        for operation_id in operation_ids:
            await self.cancel(operation_id)
        async with self._lock:
            tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, operation_id: str) -> None:
        operation = await self.store.get_internal(operation_id)
        if operation is None:
            return
        workspace = await self._workspace(operation.workspace_id)
        if workspace is None:
            await self.store.transition(
                operation_id,
                expected_states={"QUEUED"},
                state="FAILED",
                event_type="WORKSPACE_UNAVAILABLE",
                public_error_code="WORKSPACE_NOT_FOUND",
            )
            return
        try:
            lease = await self.store.acquire_workspace_lease(
                workspace_id=workspace.id,
                holder_type="DIRECT_OPERATION",
                holder_id=operation_id,
                lease_ms=(MAX_EXECUTION_SECONDS + 15) * 1_000,
            )
        except WorkspaceBusy as exc:
            await self.store.transition(
                operation_id,
                expected_states={"QUEUED"},
                state="REJECTED",
                event_type="LEASE_REJECTED",
                public_error_code=exc.code,
            )
            return
        try:
            running = await self.store.transition(
                operation_id,
                expected_states={"QUEUED"},
                state="RUNNING",
                event_type="EXECUTOR_STARTED",
                lease_fencing_token=lease.fencing_token,
            )
            if running is None:
                return
            if operation.kind == "RUN_CODE_BLOCK":
                result = await self._run_code_block(operation.request, workspace.path, operation_id)
            elif operation.kind == "SSH_EXECUTE":
                result = await self._run_ssh(operation.request, operation_id)
            else:
                await self.store.transition(
                    operation_id,
                    expected_states={"RUNNING"},
                    state="FAILED",
                    event_type="EXECUTOR_KIND_REJECTED",
                    public_error_code="UNSUPPORTED_OPERATION_KIND",
                )
                return
            current = await self.store.get_internal(operation_id)
            if current is not None and current.state == "CANCEL_REQUESTED":
                await self.store.complete_cancel(operation_id, detail="process terminated and reaped")
                return
            await self.store.transition(
                operation_id,
                expected_states={"RUNNING"},
                state="SUCCEEDED" if result["exit_code"] == 0 else "FAILED",
                event_type="EXECUTOR_COMPLETED",
                public_result=result,
                public_error_code=None if result["exit_code"] == 0 else "EXECUTOR_EXIT_NONZERO",
            )
        except asyncio.CancelledError:
            current = await self.store.get_internal(operation_id)
            if current is not None and current.state == "CANCEL_REQUESTED":
                await self.store.complete_cancel(operation_id, detail="executor task cancelled")
            else:
                await self.store.transition(
                    operation_id,
                    expected_states={"RUNNING", "QUEUED"},
                    state="ORPHANED",
                    event_type="EXECUTOR_CANCELLED_UNEXPECTEDLY",
                    public_error_code="RECOVERY_REQUIRED",
                )
            raise
        except ExecutorUnavailable as exc:
            await self.store.transition(
                operation_id,
                expected_states={"RUNNING", "QUEUED"},
                state="REJECTED",
                event_type="EXECUTOR_UNAVAILABLE",
                public_error_code=exc.code,
            )
        except InvalidSshProfile as exc:
            await self.store.transition(
                operation_id,
                expected_states={"RUNNING", "QUEUED"},
                state="REJECTED",
                event_type="SSH_PROFILE_REJECTED",
                public_error_code=exc.code,
            )
        except (OSError, TypeError, ValueError):
            await self.store.transition(
                operation_id,
                expected_states={"RUNNING", "QUEUED"},
                state="FAILED",
                event_type="EXECUTOR_FAILED",
                public_error_code="EXECUTOR_FAILED",
            )
        finally:
            await self.store.release_workspace_lease(
                workspace_id=workspace.id,
                holder_type="DIRECT_OPERATION",
                holder_id=operation_id,
                fencing_token=lease.fencing_token,
            )
            async with self._lock:
                self._tasks.pop(operation_id, None)
                self._processes.pop(operation_id, None)

    async def _run_code_block(
        self, request: dict[str, Any], workspace_path: str, operation_id: str
    ) -> dict[str, Any]:
        language = str(request.get("language") or "")
        code = str(request.get("code") or "")
        if language not in {"python", "javascript", "typescript", "bash"}:
            raise ExecutorUnavailable("unsupported code-block language")
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            raise ExecutorUnavailable("code block too large")
        configured_runner = os.environ.get("CPTR_DIRECT_CODE_SANDBOX_RUNNER", "").strip()
        runner = Path(configured_runner) if configured_runner else Path(__file__).with_name(
            "direct_sandbox_runner.py"
        )
        if not runner.is_file():
            raise ExecutorUnavailable("no trusted sandbox runner configured")
        runner_argv = [str(runner)] if configured_runner else [sys.executable, str(runner)]
        runner_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        node_runtime = os.environ.get("CPTR_DIRECT_CODE_NODE_RUNTIME") or shutil.which("node")
        if node_runtime and Path(node_runtime).is_file():
            runner_env["CPTR_DIRECT_CODE_NODE_RUNTIME"] = str(Path(node_runtime).resolve())
        process = await asyncio.create_subprocess_exec(
            *runner_argv,
            "--language",
            language,
            "--workspace",
            workspace_path,
            "--timeout-seconds",
            str(MAX_EXECUTION_SECONDS),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=runner_env,
        )
        async with self._lock:
            self._processes[operation_id] = process
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(code.encode("utf-8")), timeout=MAX_EXECUTION_SECONDS + 5
            )
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            return {"executor": "sandbox", "exit_code": None, "timed_out": True, "stdout": _bounded(stdout), "stderr": _bounded(stderr)}
        return {"executor": "sandbox", "exit_code": process.returncode, "timed_out": False, "stdout": _bounded(stdout), "stderr": _bounded(stderr)}

    async def _run_ssh(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]:
        profile = _profile(str(request.get("ssh_profile") or ""))
        ssh_action = str(request.get("ssh_action") or "")
        if not _SAFE_ACTION_ID.fullmatch(ssh_action):
            raise InvalidSshProfile("SSH action is missing or invalid")
        remote_command = profile.actions.get(ssh_action)
        if remote_command is None:
            raise InvalidSshProfile("SSH action is not allowed by this profile")
        process = await asyncio.create_subprocess_exec(
            "ssh",
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={profile.known_hosts_file}",
            "-o",
            "ConnectTimeout=10",
            "-i",
            profile.identity_file,
            "-p",
            str(profile.port),
            f"{profile.user}@{profile.host}",
            remote_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        async with self._lock:
            self._processes[operation_id] = process
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=MAX_EXECUTION_SECONDS
            )
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            return {"executor": "ssh", "profile_id": profile.profile_id, "action_id": ssh_action, "exit_code": None, "timed_out": True, "stdout": _bounded(stdout), "stderr": _bounded(stderr)}
        return {"executor": "ssh", "profile_id": profile.profile_id, "action_id": ssh_action, "exit_code": process.returncode, "timed_out": False, "stdout": _bounded(stdout), "stderr": _bounded(stderr)}

    @staticmethod
    async def _workspace(workspace_id: str) -> Workspace | None:
        async with await get_db() as db:
            return await db.get(Workspace, workspace_id)
