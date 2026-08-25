"""Bounded, content-based workspace snapshots for autonomous evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cptr.utils.git import GitError, _run
from cptr.utils.identity import ExecutionIdentity

MAX_FILES = 2_000
MAX_TOTAL_BYTES = 25 * 1024 * 1024
MAX_HASH_BYTES_PER_FILE = 5 * 1024 * 1024
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".config",
    "browser-data",
    "browser-profile",
    "secrets",
    "credentials",
}
EXCLUDED_FILES = {
    "cookies",
    "Cookies",
    "History",
    "Login Data",
    "Local State",
}


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _hash_file(path: Path, limit: int) -> tuple[str, int, bool]:
    digest = hashlib.sha256()
    size = 0
    truncated = False
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(min(1024 * 1024, limit - size))
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if size == limit and handle.read(1):
                truncated = True
                break
    return digest.hexdigest(), path.stat().st_size, truncated


async def _git_paths(root: Path, identity: ExecutionIdentity | None) -> list[str] | None:
    try:
        code, output, _ = await _run(
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            cwd=str(root),
            check=False,
            identity=identity,
        )
    except GitError:
        return None
    if code != 0:
        return None
    return [item for item in output.split("\0") if item]


def _fallback_paths(root: Path) -> list[str]:
    paths: list[str] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [item for item in directories if item not in EXCLUDED_PARTS]
        current_path = Path(current)
        for filename in filenames:
            path = current_path / filename
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            paths.append(relative.as_posix())
            if len(paths) >= MAX_FILES:
                return paths
    return paths


async def snapshot_workspace(
    root: str, identity: ExecutionIdentity | None = None
) -> dict[str, Any]:
    """Return a bounded content fingerprint of relevant workspace files.

    Git-aware enumeration includes tracked and non-ignored untracked files, which
    makes edits to an existing untracked fixture visible. A bounded filesystem
    fallback supports non-Git disposable workspaces without traversing excluded
    dependency, cache, build, or VCS directories.
    """
    root_path = Path(root).resolve()
    paths = await _git_paths(root_path, identity)
    if paths is None:
        paths = _fallback_paths(root_path)
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    truncated = len(paths) > MAX_FILES
    for relative_text in sorted(set(paths))[:MAX_FILES]:
        relative = Path(relative_text)
        if relative.is_absolute() or any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if (
            relative.name in EXCLUDED_FILES
            or relative.name == ".env"
            or relative.name.startswith(".env.")
        ):
            continue
        raw_candidate = root_path / relative
        if raw_candidate.is_symlink():
            continue
        candidate = raw_candidate.resolve(strict=False)
        if not _inside(root_path, candidate) or not candidate.is_file():
            continue
        if total_bytes >= MAX_TOTAL_BYTES:
            truncated = True
            break
        hash_limit = min(MAX_HASH_BYTES_PER_FILE, MAX_TOTAL_BYTES - total_bytes)
        file_hash, actual_size, file_truncated = await asyncio.to_thread(
            _hash_file, candidate, hash_limit
        )
        total_bytes += min(actual_size, hash_limit)
        entry: dict[str, Any] = {
            "path": relative.as_posix(),
            "sha256": file_hash,
            "size": actual_size,
        }
        if file_truncated:
            entry["content_truncated"] = True
            truncated = True
        entries.append(entry)
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return {
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "files": entries,
        "file_count": len(entries),
        "truncated": truncated,
    }


def changed_paths(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[str]:
    """Return bounded relative paths whose snapshot entry changed."""
    if not before or not after:
        return []
    before_files = {str(item.get("path")): item for item in before.get("files", [])}
    after_files = {str(item.get("path")): item for item in after.get("files", [])}
    return sorted(
        path
        for path in set(before_files) | set(after_files)
        if before_files.get(path) != after_files.get(path)
    )[:MAX_FILES]
