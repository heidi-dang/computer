"""Hermetic, read-only Git inspection contracts for FlowDeck specialists.

This module intentionally does not expose arbitrary Git arguments or a shell.
All operations are fixed, bounded queries scoped to one owned workspace.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_BYTES = 128 * 1024
MAX_LOG_ENTRIES = 50
MAX_DIFF_BYTES = 96 * 1024
INSPECTION_TIMEOUT_SECONDS = 15


class GitInspectionError(RuntimeError):
    """Raised when a structured Git inspection cannot be completed safely."""


@dataclass(frozen=True)
class GitInspectionRequest:
    workspace: str
    operation: str
    limit: int = 20


def _root(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise GitInspectionError("workspace is not a directory")
    return root


def validate_git_request(request: GitInspectionRequest, *, authorized_workspace: str) -> Path:
    root = _root(request.workspace)
    authorized = _root(authorized_workspace)
    if root != authorized:
        raise GitInspectionError("workspace is outside the authorized FlowDeck scope")
    if request.operation not in {"status", "log", "diff"}:
        raise GitInspectionError("Git operation is not in the read-only allowlist")
    if request.limit < 1 or request.limit > MAX_LOG_ENTRIES:
        raise GitInspectionError("Git result limit is outside the safe bound")
    if not (root / ".git").exists():
        raise GitInspectionError("workspace is not a Git repository")
    return root


async def _git(root: Path, *args: str, output_limit: int = MAX_OUTPUT_BYTES) -> str:
    """Run one fixed read-only Git query without a shell or inherited secrets."""
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(root),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "--no-optional-locks",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
        },
    )
    async def read_bounded(stream: asyncio.StreamReader) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > output_limit:
                raise GitInspectionError("Git output exceeded configured bound")
            chunks.append(chunk)

    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.gather(read_bounded(process.stdout), read_bounded(process.stderr)),
            timeout=INSPECTION_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise GitInspectionError("Git inspection timed out") from None
    except GitInspectionError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    if len(stdout) > output_limit or len(stderr) > output_limit:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise GitInspectionError("Git output exceeded configured bound")
    await process.wait()
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise GitInspectionError(detail or "Git inspection failed")
    return stdout.decode(errors="replace")


async def inspect_git(request: GitInspectionRequest, *, authorized_workspace: str) -> dict:
    """Execute one bounded structured Git inspection."""
    root = validate_git_request(request, authorized_workspace=authorized_workspace)
    if request.operation == "status":
        raw = await _git(root, "status", "--short", "--branch")
        lines = raw.splitlines()[:MAX_LOG_ENTRIES]
        return {"operation": "status", "workspace": str(root), "lines": lines}
    if request.operation == "log":
        raw = await _git(
            root,
            "log",
            f"-{request.limit}",
            "--date=iso-strict",
            "--format=%H%x09%aI%x09%an%x09%s",
            output_limit=MAX_OUTPUT_BYTES,
        )
        entries = []
        for line in raw.splitlines()[: request.limit]:
            parts = line.split("\t", 3)
            if len(parts) == 4:
                entries.append(
                    {"commit": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3]}
                )
        return {"operation": "log", "workspace": str(root), "entries": entries}
    raw = await _git(
        root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        output_limit=MAX_DIFF_BYTES,
    )
    return {
        "operation": "diff",
        "workspace": str(root),
        "diff": raw,
        "truncated": False,
    }