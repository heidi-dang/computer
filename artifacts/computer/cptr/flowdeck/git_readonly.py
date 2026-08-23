"""Hermetic, read-only Git inspection for FlowDeck specialists."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cptr.flowdeck.git import GitInspectionError
from cptr.flowdeck.git import GitInspectionRequest as SecureGitInspectionRequest
from cptr.flowdeck.git import inspect_git as secure_inspect_git


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


async def inspect_git(request: GitInspectionRequest, *, authorized_workspace: str) -> str:
    """Run one fixed read-only Git operation with bounded output."""
    if request.operation not in _OPERATIONS:
        raise GitInspectionPolicyError("unsupported Git inspection operation")
    if request.limit < 1 or request.limit > 100_000:
        raise GitInspectionPolicyError("Git inspection output limit is unsafe")
    try:
        result = await secure_inspect_git(
            SecureGitInspectionRequest(
                workspace=request.workspace,
                operation="diff" if request.operation == "diff_stat" else request.operation,
                limit=min(request.limit, 50),
            ),
            authorized_workspace=authorized_workspace,
        )
    except GitInspectionError as exc:
        raise GitInspectionPolicyError(str(exc)) from exc
    if request.operation == "status":
        return "\n".join(result["lines"])
    if request.operation == "log":
        return "\n".join(
            f'{entry["commit"][:12]} {entry["subject"]}' for entry in result["entries"]
        )
    return result["diff"]