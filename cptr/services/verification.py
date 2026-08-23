"""Independent verification contracts for autonomous supervisor decisions."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

MAX_COMMAND_OUTPUT_CHARS = 8_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120.0
VERIFICATION_CATEGORIES = frozenset(
    {
        "focused_tests",
        "broader_tests",
        "lint",
        "typecheck",
        "build",
        "runtime_smoke",
    }
)


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VerificationCommand:
    """One configured, argv-based validation command."""

    name: str
    argv: tuple[str, ...]
    category: str = "runtime_smoke"
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS


class IndependentVerifier(Protocol):
    async def verify(
        self, *, task: dict[str, Any], evidence: dict[str, Any], **kwargs: Any
    ) -> VerificationResult: ...


class DefaultIndependentVerifier:
    """Verify durable state and configured workspace invariants independently."""

    def __init__(self, *, commands: list[VerificationCommand] | None = None) -> None:
        self._commands = commands

    async def verify(
        self, *, task: dict[str, Any], evidence: dict[str, Any], **kwargs: Any
    ) -> VerificationResult:
        checks: list[dict[str, Any]] = []
        failures: list[str] = []
        terminal_success = str(task.get("status") or "").upper() in {
            "COMPLETE",
            "COMPLETED",
            "SUCCEEDED",
        }
        checks.append({"name": "durable_terminal_success", "passed": terminal_success})
        if not terminal_success:
            failures.append("worker did not reach a successful terminal state")

        independent = evidence.get("independent") or {}
        diff_check = independent.get("git_diff_check") or {}
        diff_passed = diff_check.get("passed", True)
        checks.append({"name": "git_diff_check", "passed": bool(diff_passed)})
        if not diff_passed:
            failures.append("git diff --check reported errors")

        try:
            commands = self._commands
            if commands is None:
                commands = _commands_from_environment()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            checks.append(
                {
                    "name": "verification_configuration",
                    "passed": False,
                    "error": str(exc),
                }
            )
            failures.append(f"invalid verification command configuration: {exc}")
            commands = []

        workspace_root = independent.get("workspace_path") or kwargs.get("workspace_root")
        if commands and not workspace_root:
            checks.append(
                {
                    "name": "verification_workspace",
                    "passed": False,
                    "error": "workspace path is required for configured verification commands",
                }
            )
            failures.append("workspace path is required for configured verification commands")
        elif commands:
            for command in commands:
                check = await _run_command(command, Path(workspace_root))
                checks.append(check)
                if not check["passed"]:
                    detail = (
                        "timed out" if check["timed_out"] else f"exit code {check['exit_code']}"
                    )
                    failures.append(f"{command.name} failed ({detail})")

        return VerificationResult(passed=not failures, checks=checks, failures=failures)


def _commands_from_environment() -> list[VerificationCommand]:
    raw = os.environ.get("CPTR_VERIFICATION_COMMANDS_JSON", "").strip()
    if not raw:
        return []
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise TypeError("CPTR_VERIFICATION_COMMANDS_JSON must be a JSON array")
    return _commands_from_payload(payload)


def _commands_from_payload(payload: list[Any]) -> list[VerificationCommand]:
    commands: list[VerificationCommand] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise TypeError(f"verification command {index} must be an object")
        name = str(item.get("name") or "").strip()
        argv = item.get("argv")
        if (
            not name
            or not isinstance(argv, list)
            or not argv
            or not all(isinstance(part, str) and part for part in argv)
        ):
            raise ValueError(f"verification command {index} requires name and non-empty argv")
        timeout = float(item.get("timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS))
        if timeout <= 0 or timeout > 600:
            raise ValueError(
                f"verification command {name} timeout must be between 0 and 600 seconds"
            )
        category = str(item.get("category") or "runtime_smoke").strip()
        if category not in VERIFICATION_CATEGORIES:
            allowed = ", ".join(sorted(VERIFICATION_CATEGORIES))
            raise ValueError(
                f"unknown verification category {category!r}; expected one of {allowed}"
            )
        commands.append(
            VerificationCommand(
                name=name,
                argv=tuple(argv),
                category=category,
                timeout_seconds=timeout,
            )
        )
    return commands


async def _run_command(command: VerificationCommand, workspace_root: Path) -> dict[str, Any]:
    started_at = int(time.time() * 1000)
    timed_out = False
    process: asyncio.subprocess.Process | None = None
    stdout = b""
    stderr = b""
    exit_code: int | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=str(workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=command.timeout_seconds
            )
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        exit_code = process.returncode
    except OSError as exc:
        stderr = str(exc).encode()
    finished_at = int(time.time() * 1000)
    passed = not timed_out and exit_code == 0
    return {
        "name": command.name,
        "category": command.category,
        "verification_command": True,
        "argv": list(command.argv),
        "cwd": str(workspace_root),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": max(0, finished_at - started_at),
        "exit_code": exit_code,
        "stdout": stdout.decode(errors="replace")[-MAX_COMMAND_OUTPUT_CHARS:],
        "stderr": stderr.decode(errors="replace")[-MAX_COMMAND_OUTPUT_CHARS:],
        "timed_out": timed_out,
        "passed": passed,
    }
