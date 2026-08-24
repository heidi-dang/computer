"""Bounded Git worktree lifecycle for CPTR Build mutation nodes.

The canonical workspace remains the authenticated authority. Mutation children
may only run in task-owned, branch-backed worktrees created from one common
base; integration is an explicit, bounded cherry-pick operation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cptr.utils.git import _run, worktrees


class WorktreeLifecycleError(RuntimeError):
    """Raised when a worktree lifecycle invariant cannot be established."""


@dataclass(frozen=True)
class BuildWorktree:
    canonical_workspace: str
    node_key: str
    branch: str
    path: str
    common_base: str


def _safe_component(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return result or "node"


async def _canonical_root(workspace: str) -> Path:
    requested = Path(workspace).expanduser()
    if requested.is_symlink():
        raise WorktreeLifecycleError("canonical workspace may not be a symlink")
    if not requested.is_dir():
        raise WorktreeLifecycleError("canonical workspace is not a directory")
    _, output, _ = await _run("rev-parse", "--show-toplevel", cwd=str(requested))
    root = Path(output.strip()).resolve()
    if root != requested.resolve():
        raise WorktreeLifecycleError("canonical workspace must be the repository root")
    return root


async def common_base(canonical_workspace: str) -> str:
    root = await _canonical_root(canonical_workspace)
    _, output, _ = await _run("rev-parse", "HEAD", cwd=str(root))
    base = output.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise WorktreeLifecycleError("repository HEAD is not a full immutable commit")
    return base


async def create_worktree(
    *,
    canonical_workspace: str,
    run_id: str,
    node_key: str,
    common_base: str | None = None,
) -> BuildWorktree:
    root = await _canonical_root(canonical_workspace)
    base = common_base or await common_base_for(root)
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise WorktreeLifecycleError("worktree base must be an immutable commit")
    run_part = _safe_component(run_id)[:24]
    node_part = _safe_component(node_key)[:48]
    branch = f"cptr/{run_part}/{node_part}"
    path = root.parent / f".cptr-worktree-{run_part}-{node_part}"
    if path.exists() or path.is_symlink():
        raise WorktreeLifecycleError("task-owned worktree path already exists")
    await _run(
        "worktree",
        "add",
        "-b",
        branch,
        str(path),
        base,
        cwd=str(root),
    )
    return BuildWorktree(str(root), node_key, branch, str(path), base)


async def common_base_for(root: Path) -> str:
    _, output, _ = await _run("rev-parse", "HEAD", cwd=str(root))
    return output.strip()


async def validate_execution_worktree(
    canonical_workspace: str,
    execution_workspace: str,
) -> Path:
    root = await _canonical_root(canonical_workspace)
    target = Path(execution_workspace).expanduser()
    if target.is_symlink():
        raise ValueError("execution workspace may not be a symlink")
    target = target.resolve()
    if target == root:
        return root
    if not target.is_dir():
        raise ValueError("execution workspace is not a directory")
    listing = await worktrees(str(root))
    for item in listing["worktrees"]:
        item_path = Path(str(item["path"])).resolve()
        if item_path == target and not Path(str(item["path"])).is_symlink():
            return target
    raise ValueError("execution workspace is not a worktree of the authenticated repository")


async def worktree_changed_paths(handle: BuildWorktree) -> tuple[str, ...]:
    await validate_execution_worktree(handle.canonical_workspace, handle.path)
    _, tracked, _ = await _run(
        "diff",
        "--name-only",
        handle.common_base,
        cwd=handle.path,
    )
    _, untracked, _ = await _run(
        "ls-files",
        "--others",
        "--exclude-standard",
        cwd=handle.path,
    )
    paths = {line.strip() for line in (tracked + untracked).splitlines() if line.strip()}
    return tuple(sorted(paths))


async def canonical_changed_paths(
    canonical_workspace: str,
    common_base: str,
) -> tuple[str, ...]:
    root = await _canonical_root(canonical_workspace)
    _, tracked, _ = await _run(
        "diff",
        "--name-only",
        common_base,
        cwd=str(root),
    )
    _, untracked, _ = await _run(
        "ls-files",
        "--others",
        "--exclude-standard",
        cwd=str(root),
    )
    paths = {line.strip() for line in (tracked + untracked).splitlines() if line.strip()}
    return tuple(sorted(paths))


def _protected(path: str) -> bool:
    parts = Path(path).parts
    return any(part in {".git", ".env", ".cptr", "secrets"} for part in parts)


async def commit_worktree(handle: BuildWorktree, message: str) -> str | None:
    paths = await worktree_changed_paths(handle)
    if any(_protected(path) for path in paths):
        raise WorktreeLifecycleError("mutation attempted to include a protected path")
    if not paths:
        return None
    await _run("add", "--all", "--", ".", cwd=handle.path)
    env = {
        "GIT_AUTHOR_NAME": "CPTR Build Agent",
        "GIT_AUTHOR_EMAIL": "cptr-build-agent@example.invalid",
        "GIT_COMMITTER_NAME": "CPTR Build Agent",
        "GIT_COMMITTER_EMAIL": "cptr-build-agent@example.invalid",
    }
    await _run("commit", "-m", message, cwd=handle.path, extra_env=env)
    _, output, _ = await _run("rev-parse", "HEAD", cwd=handle.path)
    return output.strip()


async def integrate_worktree(
    canonical_workspace: str,
    handle: BuildWorktree,
    commit_hash: str | None,
) -> dict[str, Any]:
    root = await _canonical_root(canonical_workspace)
    if root != Path(handle.canonical_workspace).resolve():
        raise WorktreeLifecycleError("integration repository does not match worktree owner")
    if not commit_hash:
        return {"status": "succeeded", "changed_paths": []}
    code, output, error = await _run(
        "cherry-pick",
        commit_hash,
        cwd=str(root),
        check=False,
    )
    if code == 0:
        return {"status": "succeeded", "commit": commit_hash}
    await _run("cherry-pick", "--abort", cwd=str(root), check=False)
    return {
        "status": "conflict",
        "commit": commit_hash,
        "error": (error or output).strip()[:1000],
    }


async def remove_worktree(handle: BuildWorktree) -> None:
    root = await _canonical_root(handle.canonical_workspace)
    target = Path(handle.path).resolve()
    if target == root or target.parent != root.parent:
        raise WorktreeLifecycleError("refusing to remove an unbounded worktree path")
    await _run("worktree", "remove", "--force", str(target), cwd=str(root), check=False)
    await _run("branch", "-D", handle.branch, cwd=str(root), check=False)


async def list_task_worktrees(canonical_workspace: str, run_id: str) -> list[dict[str, str]]:
    root = await _canonical_root(canonical_workspace)
    prefix = f"cptr/{_safe_component(run_id)[:24]}/"
    result = []
    listing = await worktrees(str(root))
    for item in listing["worktrees"]:
        branch = str(item.get("branch") or "")
        path = str(item.get("path") or "")
        if branch.startswith(prefix) and path:
            result.append({"branch": branch, "path": path})
    return result

