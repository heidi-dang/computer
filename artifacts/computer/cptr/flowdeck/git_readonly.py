"""Hermetic, read-only Git inspection for FlowDeck specialists."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


class GitInspectionPolicyError(RuntimeError):
    """Raised when a structured Git inspection cannot be safely performed."""


@dataclass(frozen=True)
class GitInspectionRequest:
    operation: str
    workspace: str
    limit: int = 100


_OPERATIONS = {
    "status": ("status", "--short", "--branch", "--untracked-files=no"),
    "log": ("log", "--oneline", "--no-decorate"),
    "diff_stat": ("diff", "--no-ext-diff", "--no-textconv", "--stat"),
}


def _workspace(path: str) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise GitInspectionPolicyError("Git workspace is not a directory")
    return root


def _environment(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "true",
    }


async def inspect_git(request: GitInspectionRequest) -> str:
    """Run one fixed read-only Git operation with bounded output."""
    if request.operation not in _OPERATIONS:
        raise GitInspectionPolicyError("unsupported Git inspection operation")
    if request.limit < 1 or request.limit > 100_000:
        raise GitInspectionPolicyError("Git inspection output limit is unsafe")
    root = _workspace(request.workspace)
    executable = shutil.which("git")
    if not executable:
        raise GitInspectionPolicyError("Git executable is unavailable")
    args = list(_OPERATIONS[request.operation])
    if request.operation == "log":
        args.extend(("--max-count", str(min(request.limit, 1000))))
    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        cwd=str(root),
        env=_environment(root),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise GitInspectionPolicyError("Git inspection timed out") from None
    if process.returncode != 0:
        raise GitInspectionPolicyError(stderr.decode(errors="replace")[:1000] or "Git inspection failed")
    return stdout.decode(errors="replace")[: request.limit]