"""Read-only workspace health classification and conservative state reconciliation.

Enforces Workspace OS v2 safety policies:
- Health classification is strictly read-only.
- Multi-signal worker classification: active_overlap, stale_noop, stale_with_changes,
  already_integrated_unclosed, missing_worktree, closed, unrelated, ready.
- Worktree-noise classification distinguishes genuine source modifications from
  untracked worker worktree checkouts (.cptr-worktrees, .cptr/worktrees).
- Conservative reconcile ONLY refreshes and marks state in DB/memory; NEVER deletes
  worktrees, branches, database records, or uncommitted user work.
"""

from __future__ import annotations

import time
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import select

from cptr.models import DirectCodingWorker, Workspace
from cptr.services.direct_coding_workers import worker_changed_paths
from cptr.services.workspace_availability import is_workspace_available
from cptr.utils.db import get_db
from cptr.utils.git import (
    _run,
    is_repo,
    repository_root,
    status as git_status,
    worktrees as git_worktrees,
)
from cptr.utils.identity import ExecutionIdentity, identity_for_user_id
from cptr.utils.tools import command_sessions


class WorkerClassification(str, Enum):
    ACTIVE_OVERLAP = "active_overlap"
    STALE_NOOP = "stale_noop"
    STALE_WITH_CHANGES = "stale_with_changes"
    ALREADY_INTEGRATED_UNCLOSED = "already_integrated_unclosed"
    MISSING_WORKTREE = "missing_worktree"
    CLOSED = "closed"
    UNRELATED = "unrelated"
    READY = "ready"


class HealthBand(str, Enum):
    HEALTHY = "healthy"
    MODERATE = "moderate"
    UNHEALTHY = "unhealthy"


def is_worktree_noise_path(
    path_str: str,
    repo_root: Path | None = None,
    known_worktree_paths: set[str] | None = None,
    status: str | None = None,
) -> bool:
    """Return True if path is recognized as worktree/worker noise rather than tracked source."""
    normalized = path_str.replace("\\", "/").strip().lstrip("/")
    if (
        normalized == ".cptr-worktrees"
        or normalized.startswith(".cptr-worktrees/")
        or "/.cptr-worktrees/" in f"/{normalized}/"
    ):
        return True
    if (
        normalized == ".cptr"
        or normalized.startswith(".cptr/")
        or normalized.startswith(".cptr/worktrees")
    ):
        return True

    # Current CPTR generated-state rules (post-5356492):
    # Untracked .fdx trees are CPTR-owned derived state and treated as generated noise.
    # Tracked changes, including tracked files under .fdx, still fail closed.
    if status == "untracked" and (
        normalized == ".fdx"
        or normalized.startswith(".fdx/")
        or normalized == ".fdx-index"
        or normalized.startswith(".fdx-index/")
    ):
        return True

    if known_worktree_paths:
        for known in known_worktree_paths:
            norm_known = known.replace("\\", "/").strip().lstrip("/").rstrip("/")
            if normalized == norm_known or normalized.startswith(f"{norm_known}/"):
                return True

    if repo_root is not None:
        try:
            full_path = (repo_root / path_str).resolve()
            if known_worktree_paths:
                for known in known_worktree_paths:
                    try:
                        resolved_known = Path(known).resolve()
                        if full_path == resolved_known or resolved_known in full_path.parents:
                            return True
                    except Exception:
                        pass

            check_paths = [full_path] if full_path.is_dir() else []
            check_paths.extend(full_path.parents)
            for d in check_paths:
                if d == repo_root or d in repo_root.parents:
                    break
                git_marker = d / ".git"
                if git_marker.is_file():
                    try:
                        content = git_marker.read_text(encoding="utf-8", errors="ignore")
                        if "gitdir:" in content:
                            return True
                    except OSError:
                        pass
        except Exception:
            pass

    return False


def classify_worktree_noise(
    files: list[dict[str, Any]],
    repo_root: Path | None = None,
    known_worktree_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Classify repository files into worktree-noise vs real source changes."""
    noise_paths: list[str] = []
    real_changes: list[str] = []

    for f in files:
        if isinstance(f, str):
            p = f
            status_val = None
        else:
            p = str(f.get("path") or "")
            status_val = str(f.get("status") or "") if "status" in f else None
        if not p:
            continue
        if is_worktree_noise_path(
            p,
            repo_root=repo_root,
            known_worktree_paths=known_worktree_paths,
            status=status_val,
        ):
            noise_paths.append(p)
        else:
            real_changes.append(p)

    return {
        "has_noise": len(noise_paths) > 0,
        "noise_paths": sorted(noise_paths),
        "real_changes": sorted(real_changes),
        "is_clean_strict": len(files) == 0,
        "is_clean_excluding_noise": len(real_changes) == 0,
    }


async def is_branch_merged_into_head(
    repo_root: Path | str,
    branch_name: str,
    identity: ExecutionIdentity | None = None,
) -> bool:
    """Return True if branch_name exists and is an ancestor of HEAD."""
    if not branch_name:
        return False
    try:
        code, _, _ = await _run(
            "merge-base",
            "--is-ancestor",
            branch_name,
            "HEAD",
            cwd=str(repo_root),
            check=False,
            identity=identity,
        )
        return code == 0
    except Exception:
        return False


def _get_worker_active_command_ids(worktree_path: Path) -> tuple[list[str], list[str]]:
    """Return (active_command_ids, recent_command_ids) for a worker worktree."""
    resolved_root = worktree_path.resolve()
    sessions = [
        session
        for session in command_sessions.values()
        if Path(str(session.get("workspace") or "")).resolve() == resolved_root
    ]
    sessions.sort(key=lambda item: float(item.get("created_at") or 0))
    active = [str(item.get("id")) for item in sessions if not item.get("done") and item.get("id")]
    recent = [str(item.get("id")) for item in sessions[-5:] if item.get("id")]
    return active, recent


def _is_detached_head(branch: str) -> bool:
    """Return True when git reports HEAD as detached (branch.head == "(detached)")."""
    clean = (branch or "").strip()
    if not clean:
        return False
    return clean in {"(detached)", "(no branch)", "HEAD (no branch)"} or clean.startswith(
        "(detached"
    )


def _check_fdx_index_staleness(repo_root: Path) -> dict[str, Any]:
    """Lightweight check for a stale FDX/index state without spawning the fdx daemon.

    Returns a dict with 'stale' bool, 'reason' string, and 'index_path'.
    Stale when an fdx index marker exists and is older than the most recent commit
    marker (COMMIT_EDITMSG, logs/HEAD) by more than 5 seconds.
    """
    fdx_candidates = [
        repo_root / ".fdx" / "index",
        repo_root / ".git" / "fdx-index",
        repo_root / ".fdx-index",
    ]

    git_dir: Path | None = None
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        git_dir = dot_git
    elif dot_git.is_file():
        try:
            content = dot_git.read_text(encoding="utf-8", errors="ignore")
            if "gitdir:" in content:
                raw_gitdir = content.split("gitdir:", 1)[1].strip()
                target = Path(raw_gitdir)
                git_dir = target if target.is_absolute() else (repo_root / target).resolve()
        except OSError:
            pass

    for candidate in fdx_candidates:
        if candidate.exists():
            try:
                if candidate.is_dir():
                    files = list(candidate.rglob("*"))
                    fdx_mtime = max(
                        (f.stat().st_mtime for f in files if f.is_file()),
                        default=candidate.stat().st_mtime,
                    )
                else:
                    fdx_mtime = candidate.stat().st_mtime
            except OSError:
                fdx_mtime = 0.0

            commit_mtime: float | None = None
            if git_dir is not None and git_dir.exists():
                candidate_markers = [
                    git_dir / "COMMIT_EDITMSG",
                    git_dir / "logs" / "HEAD",
                ]
                if (git_dir / "commondir").exists():
                    try:
                        common = (git_dir / "commondir").read_text(encoding="utf-8").strip()
                        common_dir = (
                            Path(common)
                            if Path(common).is_absolute()
                            else (git_dir / common).resolve()
                        )
                        candidate_markers.append(common_dir / "COMMIT_EDITMSG")
                    except OSError:
                        pass

                for marker in candidate_markers:
                    if marker.exists():
                        try:
                            m = marker.stat().st_mtime
                            if commit_mtime is None or m > commit_mtime:
                                commit_mtime = m
                        except OSError:
                            pass

            if commit_mtime is not None:
                if commit_mtime > fdx_mtime + 5:  # 5s tolerance
                    return {
                        "stale": True,
                        "reason": (
                            f"fdx index at {candidate} is older than last commit "
                            f"(index_mtime={fdx_mtime:.0f}, commit_mtime={commit_mtime:.0f})"
                        ),
                        "index_path": str(candidate),
                    }
                return {
                    "stale": False,
                    "reason": "fdx index up-to-date",
                    "index_path": str(candidate),
                }

            return {
                "stale": False,
                "reason": f"fdx index at {candidate} exists; no newer commit marker found",
                "index_path": str(candidate),
            }

    return {"stale": False, "reason": "no fdx index found", "index_path": None}


async def _check_origin_default_branch_mismatch(
    repo_root: Path,
    identity: ExecutionIdentity | None = None,
) -> dict[str, Any]:
    """Check whether the local HEAD matches origin's default branch.

    Returns a dict with 'mismatch' bool, 'local_branch', 'origin_default', and 'reason'.
    Safe — read-only git calls only.
    """
    result: dict[str, Any] = {
        "mismatch": False,
        "local_branch": None,
        "origin_default": None,
        "reason": "not checked",
    }
    try:
        code, head_out, _ = await _run(
            "symbolic-ref",
            "--short",
            "-q",
            "HEAD",
            cwd=str(repo_root),
            check=False,
            identity=identity,
        )
        if code != 0:
            result["reason"] = "HEAD is detached; cannot compare with origin default"
            result["mismatch"] = True
            return result
        local_branch = head_out.strip()
        result["local_branch"] = local_branch

        origin_default: str | None = None
        code2, remote_out, _ = await _run(
            "ls-remote",
            "--symref",
            "origin",
            "HEAD",
            cwd=str(repo_root),
            check=False,
            identity=identity,
        )
        if code2 == 0 and remote_out:
            for line in remote_out.splitlines():
                if line.startswith("ref: refs/heads/"):
                    rem = line.removeprefix("ref: refs/heads/")
                    origin_default = rem.split()[0].strip()
                    break

        if origin_default is None:
            code_sym, sym_out, _ = await _run(
                "symbolic-ref",
                "--short",
                "-q",
                "refs/remotes/origin/HEAD",
                cwd=str(repo_root),
                check=False,
                identity=identity,
            )
            if code_sym == 0 and sym_out.strip():
                origin_default = sym_out.strip().removeprefix("origin/")

        if origin_default is None:
            code_rem, rem_list, _ = await _run(
                "remote", cwd=str(repo_root), check=False, identity=identity
            )
            if code_rem == 0 and "origin" not in rem_list.split():
                result["reason"] = "no origin remote configured"
                return result
            result["reason"] = "origin default branch pointer not found"
            return result

        result["origin_default"] = origin_default
        if local_branch != origin_default:
            result["mismatch"] = True
            result["reason"] = (
                f"local branch '{local_branch}' differs from origin default '{origin_default}'"
            )
        else:
            result["reason"] = f"local branch matches origin default '{origin_default}'"
        return result
    except Exception as exc:
        result["reason"] = f"origin/default-branch check failed: {exc}"
        return result


async def _check_duplicate_workspace_registration(
    workspace: Workspace,
    user_id: str,
) -> dict[str, Any]:
    """Detect whether multiple Workspace rows resolve to the same filesystem path for this user.

    The DB UniqueConstraint on (user_id, path) prevents exact string duplicates, but different
    path strings (e.g. with/without trailing slash, symlinks) may resolve to the same directory.
    Returns a dict with 'duplicate' bool and 'duplicates' list of ids sharing the resolved path.
    """
    try:
        resolved_target = Path(workspace.path).expanduser().resolve()
        async with await get_db() as db:
            from cptr.models import Workspace as WS

            result = await db.execute(select(WS).where(WS.user_id == user_id))
            all_ws = list(result.scalars().all())

        duplicates = [
            w.id
            for w in all_ws
            if w.id != workspace.id and Path(w.path).expanduser().resolve() == resolved_target
        ]
        return {
            "duplicate": len(duplicates) > 0,
            "duplicates": duplicates,
            "resolved_path": str(resolved_target),
        }
    except Exception as exc:
        return {"duplicate": False, "duplicates": [], "reason": str(exc)}


def _check_unfinished_command_sessions(workspace_root: Path) -> dict[str, Any]:
    """Detect orphaned (not-done) command sessions referencing this workspace.

    Returns 'orphaned_sessions' list of session ids with 'done' == False.
    These are in-memory only; the check is read-only.
    """
    try:
        from cptr.services.execution_manager import command_session_registry

        command_session_registry.reconcile()
    except Exception:
        pass

    resolved_root = workspace_root.resolve()
    orphaned: list[str] = []
    for session_id, session in command_sessions.items():
        if session.get("done"):
            continue
        session_ws = str(session.get("workspace") or "")
        if not session_ws:
            continue
        try:
            resolved_ws = Path(session_ws).resolve()
            if resolved_ws == resolved_root or resolved_root in resolved_ws.parents:
                orphaned.append(str(session_id))
        except Exception:
            pass
    return {
        "orphaned_count": len(orphaned),
        "orphaned_session_ids": orphaned,
        "has_orphaned_sessions": len(orphaned) > 0,
    }


async def classify_worker(
    worker: DirectCodingWorker,
    *,
    repo_root: Path | None = None,
    identity: ExecutionIdentity | None = None,
    now_ms: int | None = None,
    stale_threshold_seconds: float = 300.0,
    active_threshold_seconds: float = 120.0,
) -> dict[str, Any]:
    """Perform multi-signal classification for a DirectCodingWorker."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    worktree_path = Path(worker.worktree_path)
    worktree_exists = worktree_path.is_dir()

    active_commands: list[str] = []
    recent_commands: list[str] = []
    if worktree_exists:
        active_commands, recent_commands = _get_worker_active_command_ids(worktree_path)

    changed_paths: list[str] = []
    if worktree_exists and worker.status != "CLOSED":
        try:
            changed_paths = sorted(await worker_changed_paths(worktree_path, identity=identity))
        except Exception:
            changed_paths = []

    last_active_ms = worker.last_activity_at or worker.updated_at or worker.created_at or 0
    inactivity_seconds = max(0.0, (now_ms - last_active_ms) / 1000.0)

    branch_merged = False
    if repo_root is not None and worker.branch:
        branch_merged = await is_branch_merged_into_head(
            repo_root, worker.branch, identity=identity
        )

    # Multi-signal decision tree
    classification: WorkerClassification
    reasons: list[str] = []

    if worker.status == "CLOSED" or worker.closed_at is not None:
        classification = WorkerClassification.CLOSED
        reasons.append("worker marked closed")
    elif not worktree_exists:
        classification = WorkerClassification.MISSING_WORKTREE
        reasons.append(f"worktree directory missing from disk: {worker.worktree_path}")
    elif active_commands:
        classification = WorkerClassification.ACTIVE_OVERLAP
        reasons.append(f"active command running ({len(active_commands)} active: {active_commands})")
    elif worker.integrated_at is not None:
        classification = WorkerClassification.ALREADY_INTEGRATED_UNCLOSED
        reasons.append(f"worker was marked integrated at {worker.integrated_at} but remains open")
    elif len(changed_paths) == 0 and branch_merged:
        classification = WorkerClassification.ALREADY_INTEGRATED_UNCLOSED
        reasons.append(
            f"branch {worker.branch} is already merged into HEAD with no uncommitted changes"
        )
    elif inactivity_seconds < active_threshold_seconds and worker.status in {"WORKING", "RUNNING"}:
        classification = WorkerClassification.ACTIVE_OVERLAP
        reasons.append(
            f"recent activity within active threshold ({int(inactivity_seconds)}s < {int(active_threshold_seconds)}s)"
        )
    elif len(changed_paths) > 0:
        if inactivity_seconds >= stale_threshold_seconds:
            classification = WorkerClassification.STALE_WITH_CHANGES
            reasons.append(
                f"idle for {int(inactivity_seconds)}s with {len(changed_paths)} unintegrated changed files; changes must be preserved"
            )
        else:
            if worker.status in {"WORKING", "RUNNING"}:
                classification = WorkerClassification.ACTIVE_OVERLAP
                reasons.append(f"actively working with {len(changed_paths)} changed files")
            else:
                classification = WorkerClassification.READY
                reasons.append(
                    f"quiescent ready worker with {len(changed_paths)} unintegrated changed files"
                )
    else:  # len(changed_paths) == 0
        if inactivity_seconds >= stale_threshold_seconds:
            classification = WorkerClassification.STALE_NOOP
            reasons.append(
                f"idle for {int(inactivity_seconds)}s with no changed files or active commands"
            )
        else:
            classification = WorkerClassification.READY
            reasons.append("ready worker with no uncommitted changes")

    return {
        "worker_id": worker.id,
        "name": worker.name,
        "responsibility": worker.responsibility,
        "repo_path": worker.repo_path,
        "status": worker.status,
        "classification": classification.value,
        "classification_reasons": reasons,
        "signals": {
            "active_command_ids": active_commands,
            "active_command_count": len(active_commands),
            "recent_command_ids": recent_commands,
            "changed_files_count": len(changed_paths),
            "changed_paths": changed_paths[:100],
            "worktree_exists": worktree_exists,
            "worktree_path": worker.worktree_path,
            "branch": worker.branch,
            "base_revision": worker.base_revision,
            "branch_merged": branch_merged,
            "is_integrated": worker.integrated_at is not None,
            "is_closed": worker.status == "CLOSED" or worker.closed_at is not None,
            "inactivity_seconds": inactivity_seconds,
            "last_activity_at": worker.last_activity_at,
            "created_at": worker.created_at,
            "updated_at": worker.updated_at,
        },
    }


async def classify_workspace_health(
    *,
    workspace: Workspace,
    user_id: str,
    identity: ExecutionIdentity | None = None,
    stale_threshold_seconds: float = 300.0,
    active_threshold_seconds: float = 120.0,
) -> dict[str, Any]:
    """Inspect and classify workspace health across availability, git status, worktrees, and workers.

    This function is strictly read-only: it does not modify files, run mutations, or alter DB state.
    """
    if identity is None:
        identity = await identity_for_user_id(user_id)

    available = is_workspace_available(workspace)
    diagnostics: list[str] = []

    if not available:
        return {
            "workspace_id": workspace.id,
            "workspace_name": workspace.name,
            "available": False,
            "is_git_repo": False,
            "git_health": None,
            "worktrees": [],
            "workers": [],
            "health_band": HealthBand.UNHEALTHY.value,
            "summary": {
                "total_workers": 0,
                "active_overlap": 0,
                "stale_noop": 0,
                "stale_with_changes": 0,
                "already_integrated_unclosed": 0,
                "missing_worktree": 0,
                "missing_git_worktrees": 0,
                "missing_checkout": 1,
                "orphan_worktrees": 0,
                "closed": 0,
                "ready": 0,
                "unrelated": 0,
                "worktree_noise_count": 0,
                "real_changes_count": 0,
                "detached_head": 0,
                "detached_worktrees": 0,
                "fdx_stale": 0,
                "origin_branch_mismatch": 0,
                "duplicate_registration": 0,
                "orphaned_sessions": 0,
            },
            "diagnostics": ["Workspace directory does not exist or is not accessible"],
            "fdx_staleness": {"stale": False, "reason": "workspace unavailable"},
            "origin_branch_check": {"mismatch": False, "reason": "workspace unavailable"},
            "duplicate_registration": {"duplicate": False, "duplicates": []},
            "session_check": {
                "orphaned_count": 0,
                "orphaned_session_ids": [],
                "has_orphaned_sessions": False,
            },
        }

    workspace_root = Path(workspace.path).expanduser().resolve()
    is_git = await is_repo(str(workspace_root), identity)

    git_info: dict[str, Any] | None = None
    worktree_list: list[dict[str, Any]] = []
    known_worktree_paths: set[str] = set()

    # Query DB for direct coding workers
    async with await get_db() as db:
        result = await db.execute(
            select(DirectCodingWorker)
            .where(
                DirectCodingWorker.user_id == user_id,
                DirectCodingWorker.workspace_id == workspace.id,
            )
            .order_by(DirectCodingWorker.created_at)
        )
        worker_rows = list(result.scalars().all())

    for w in worker_rows:
        known_worktree_paths.add(w.worktree_path)

    if is_git:
        repo_root_path = Path(await repository_root(str(workspace_root), identity)).resolve()
        try:
            raw_status = await git_status(str(workspace_root), identity)
            files = raw_status.get("files", [])
            noise_report = classify_worktree_noise(
                files,
                repo_root=repo_root_path,
                known_worktree_paths=known_worktree_paths,
            )
            _raw_branch = raw_status.get("branch", "")
            git_info = {
                "branch": _raw_branch,
                "upstream": raw_status.get("upstream", ""),
                "ahead": raw_status.get("ahead", 0),
                "behind": raw_status.get("behind", 0),
                "total_uncommitted_files": len(files),
                "worktree_noise": noise_report,
                "is_detached_head": _is_detached_head(_raw_branch),
            }
        except Exception as exc:
            diagnostics.append(f"git status probe failed: {exc}")
            git_info = {"error": str(exc), "worktree_noise": classify_worktree_noise([])}

        try:
            raw_wts = await git_worktrees(str(workspace_root), identity)
            for item in raw_wts.get("worktrees", []):
                wt_path = Path(str(item.get("path") or ""))
                dir_exists = wt_path.is_dir()
                # Check for moved/broken gitdir link in worktree checkout
                if dir_exists:
                    git_file = wt_path / ".git"
                    if git_file.is_file():
                        try:
                            content = git_file.read_text(encoding="utf-8", errors="ignore")
                            if "gitdir:" in content:
                                raw_gitdir = content.split("gitdir:", 1)[1].strip()
                                target_p = Path(raw_gitdir)
                                check_p = (
                                    target_p if target_p.is_absolute() else (wt_path / target_p)
                                )
                                if not check_p.exists():
                                    dir_exists = False
                        except OSError:
                            pass

                is_prunable = bool(item.get("is_prunable"))
                worktree_list.append(
                    {
                        **item,
                        "directory_exists": dir_exists,
                        "is_missing": not dir_exists or is_prunable,
                    }
                )
                if not dir_exists:
                    diagnostics.append(
                        f"registered git worktree directory missing or moved: {wt_path}"
                    )
                elif is_prunable:
                    diagnostics.append(
                        f"registered git worktree prunable: {wt_path} ({item.get('prunable_reason', '')})"
                    )
        except Exception as exc:
            diagnostics.append(f"git worktree probe failed: {exc}")
    else:
        repo_root_path = None
        diagnostics.append("Workspace is not a git repository or git checkout is missing")

    # Classify all workers
    now_ms = int(time.time() * 1000)
    worker_reports: list[dict[str, Any]] = []
    summary_counts: dict[str, int] = {
        "total_workers": len(worker_rows),
        "active_overlap": 0,
        "stale_noop": 0,
        "stale_with_changes": 0,
        "already_integrated_unclosed": 0,
        "missing_worktree": 0,
        "missing_git_worktrees": 0,
        "missing_checkout": 0 if available else 1,
        "orphan_worktrees": 0,
        "closed": 0,
        "ready": 0,
        "unrelated": 0,
        "worktree_noise_count": len(git_info["worktree_noise"]["noise_paths"])
        if git_info and "worktree_noise" in git_info
        else 0,
        "real_changes_count": len(git_info["worktree_noise"]["real_changes"])
        if git_info and "worktree_noise" in git_info
        else 0,
        "detached_head": 0,
        "detached_worktrees": 0,
        "fdx_stale": 0,
        "origin_branch_mismatch": 0,
        "duplicate_registration": 0,
        "orphaned_sessions": 0,
    }

    for worker in worker_rows:
        rep = await classify_worker(
            worker,
            repo_root=repo_root_path,
            identity=identity,
            now_ms=now_ms,
            stale_threshold_seconds=stale_threshold_seconds,
            active_threshold_seconds=active_threshold_seconds,
        )
        worker_reports.append(rep)
        c_val = rep["classification"]
        if c_val in summary_counts:
            summary_counts[c_val] += 1

    # Detect orphan git worktrees (registered worktree that is not current root and not in worker DB)
    orphan_worktrees: list[dict[str, Any]] = []
    for wt in worktree_list:
        if wt.get("is_current"):
            continue
        wt_resolved = str(Path(wt.get("path", "")).resolve())
        matched_worker = next(
            (w for w in worker_rows if str(Path(w.worktree_path).resolve()) == wt_resolved),
            None,
        )
        if matched_worker is None:
            wt["is_orphan"] = True
            orphan_worktrees.append(wt)
        else:
            wt["is_orphan"] = False
            wt["worker_id"] = matched_worker.id

    missing_git_worktrees = [wt for wt in worktree_list if wt.get("is_missing")]

    # Overall health band determination
    health_band = HealthBand.HEALTHY.value

    # Evaluate moderate/unhealthy triggers
    if summary_counts["missing_worktree"] > 0:
        health_band = HealthBand.MODERATE.value
        diagnostics.append(
            f"{summary_counts['missing_worktree']} worker worktrees missing from disk"
        )

    if missing_git_worktrees:
        health_band = HealthBand.MODERATE.value
        diagnostics.append(
            f"{len(missing_git_worktrees)} registered git worktree checkout(s) missing or moved from disk"
        )

    if orphan_worktrees:
        health_band = HealthBand.MODERATE.value
        diagnostics.append(
            f"{len(orphan_worktrees)} orphan git worktree(s) detected without active worker registration"
        )

    if summary_counts["stale_with_changes"] > 0:
        health_band = HealthBand.MODERATE.value
        diagnostics.append(
            f"{summary_counts['stale_with_changes']} stale workers have unintegrated changes requiring review"
        )

    if summary_counts["stale_noop"] > 0:
        if health_band == HealthBand.HEALTHY.value:
            health_band = HealthBand.MODERATE.value
        diagnostics.append(
            f"{summary_counts['stale_noop']} stale no-op workers detected (safe to refresh)"
        )

    if summary_counts["already_integrated_unclosed"] > 0:
        if health_band == HealthBand.HEALTHY.value:
            health_band = HealthBand.MODERATE.value
        diagnostics.append(
            f"{summary_counts['already_integrated_unclosed']} unclosed workers whose changes were already integrated"
        )

    if git_info and git_info.get("worktree_noise", {}).get("real_changes"):
        if health_band == HealthBand.HEALTHY.value:
            health_band = HealthBand.MODERATE.value
        diagnostics.append(
            f"{len(git_info['worktree_noise']['real_changes'])} uncommitted source changes in working tree"
        )

    if git_info and git_info.get("worktree_noise", {}).get("has_noise"):
        diagnostics.append(
            f"{len(git_info['worktree_noise']['noise_paths'])} worktree noise paths detected (excluding from real diff)"
        )

    # --- Additional structural detections ---
    # FDX/index staleness (read-only filesystem check)
    fdx_staleness: dict[str, Any] = {"stale": False, "reason": "not a git repo"}
    if is_git and repo_root_path is not None:
        fdx_staleness = _check_fdx_index_staleness(repo_root_path)
        if fdx_staleness.get("stale"):
            diagnostics.append(f"stale FDX/index state: {fdx_staleness['reason']}")

    # Origin/default-branch mismatch
    origin_branch_check: dict[str, Any] = {"mismatch": False, "reason": "not a git repo"}
    if is_git and repo_root_path is not None:
        origin_branch_check = await _check_origin_default_branch_mismatch(
            repo_root_path, identity=identity
        )
        if origin_branch_check.get("mismatch"):
            if health_band == HealthBand.HEALTHY.value:
                health_band = HealthBand.MODERATE.value
            diagnostics.append(f"origin/default-branch mismatch: {origin_branch_check['reason']}")

    # Duplicate workspace registration (same resolved path, different DB rows)
    duplicate_check = await _check_duplicate_workspace_registration(workspace, user_id)
    if duplicate_check.get("duplicate"):
        if health_band == HealthBand.HEALTHY.value:
            health_band = HealthBand.MODERATE.value
        diagnostics.append(
            f"duplicate workspace registration detected: "
            f"{len(duplicate_check['duplicates'])} other row(s) resolve to same path "
            f"({duplicate_check['resolved_path']})"
        )

    # Unfinished command/session state in this workspace
    session_check = _check_unfinished_command_sessions(workspace_root)
    if session_check.get("has_orphaned_sessions"):
        diagnostics.append(
            f"{session_check['orphaned_count']} unfinished command session(s) in workspace: "
            f"{session_check['orphaned_session_ids']}"
        )

    # Detached HEAD detection (also adds to diagnostics / health)
    if git_info and git_info.get("is_detached_head"):
        if health_band == HealthBand.HEALTHY.value:
            health_band = HealthBand.MODERATE.value
        diagnostics.append("canonical workspace HEAD is in detached state")

    # Detached worktrees (git worktree list detected detached entries)
    detached_worktrees = [wt for wt in worktree_list if wt.get("is_detached")]
    if detached_worktrees:
        if health_band == HealthBand.HEALTHY.value:
            health_band = HealthBand.MODERATE.value
        diagnostics.append(
            f"{len(detached_worktrees)} registered worktree(s) in detached-HEAD state"
        )

    summary_counts["missing_git_worktrees"] = len(missing_git_worktrees)
    summary_counts["orphan_worktrees"] = len(orphan_worktrees)
    summary_counts["unrelated"] = len(orphan_worktrees)
    summary_counts["detached_head"] = 1 if (git_info and git_info.get("is_detached_head")) else 0
    summary_counts["detached_worktrees"] = len(detached_worktrees)
    summary_counts["fdx_stale"] = 1 if fdx_staleness.get("stale") else 0
    summary_counts["origin_branch_mismatch"] = 1 if origin_branch_check.get("mismatch") else 0
    summary_counts["duplicate_registration"] = 1 if duplicate_check.get("duplicate") else 0
    summary_counts["orphaned_sessions"] = session_check["orphaned_count"]

    return {
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "available": True,
        "is_git_repo": is_git,
        "git_health": git_info,
        "worktrees": worktree_list,
        "workers": worker_reports,
        "health_band": health_band,
        "summary": summary_counts,
        "diagnostics": diagnostics,
        "fdx_staleness": fdx_staleness,
        "origin_branch_check": origin_branch_check,
        "duplicate_registration": duplicate_check,
        "session_check": session_check,
    }


async def conservative_reconcile_workspace(
    *,
    workspace: Workspace,
    user_id: str,
    identity: ExecutionIdentity | None = None,
    stale_threshold_seconds: float = 300.0,
    active_threshold_seconds: float = 120.0,
) -> dict[str, Any]:
    """Conservatively reconcile workspace worker states without deleting or discarding anything.

    Guarantees:
    - Never deletes worktrees (remove_worktree is NEVER called).
    - Never deletes git branches (delete_branch is NEVER called).
    - Never reverts or discards uncommitted changes.
    - Never deletes DirectCodingWorker rows.
    - Only updates status/timestamps in the DB to match live reality.
    """
    if identity is None:
        identity = await identity_for_user_id(user_id)

    # Reconcile in-memory process exits first (non-destructive)
    try:
        from cptr.services.execution_manager import command_session_registry

        command_session_registry.reconcile()
    except Exception:
        pass

    workspace_root = Path(workspace.path).expanduser().resolve()
    repo_root_path = (
        Path(await repository_root(str(workspace_root), identity)).resolve()
        if await is_repo(str(workspace_root), identity)
        else None
    )

    now_ms = int(time.time() * 1000)

    # Fetch workers
    async with await get_db() as db:
        result = await db.execute(
            select(DirectCodingWorker).where(
                DirectCodingWorker.user_id == user_id,
                DirectCodingWorker.workspace_id == workspace.id,
            )
        )
        workers = list(result.scalars().all())

    refreshed_workers: list[dict[str, Any]] = []
    actions_taken: list[str] = []

    async with await get_db() as db:
        for worker in workers:
            managed = await db.get(DirectCodingWorker, worker.id)
            if managed is None:
                continue

            classification_report = await classify_worker(
                managed,
                repo_root=repo_root_path,
                identity=identity,
                now_ms=now_ms,
                stale_threshold_seconds=stale_threshold_seconds,
                active_threshold_seconds=active_threshold_seconds,
            )

            classification = classification_report["classification"]
            prev_status = managed.status
            new_status = prev_status

            # Conservative state update logic:
            # 1. If active commands exist and not marked RUNNING, mark RUNNING
            if classification == WorkerClassification.ACTIVE_OVERLAP.value:
                if (
                    classification_report["signals"]["active_command_count"] > 0
                    and prev_status != "RUNNING"
                ):
                    new_status = "RUNNING"
                    managed.status = "RUNNING"
                    managed.updated_at = now_ms
                    managed.last_activity_at = now_ms
                    actions_taken.append(
                        f"Worker {managed.id} marked RUNNING (active commands: {classification_report['signals']['active_command_ids']})"
                    )

            # 2. If marked WORKING or RUNNING, but has NO active commands running
            elif prev_status in {"WORKING", "RUNNING"}:
                if classification == WorkerClassification.STALE_NOOP.value:
                    new_status = "READY"
                    managed.status = "READY"
                    managed.updated_at = now_ms
                    actions_taken.append(
                        f"Worker {managed.id} refreshed from {prev_status} to READY (stale no-op, no active commands or changes)"
                    )
                elif classification == WorkerClassification.STALE_WITH_CHANGES.value:
                    # Refresh status to READY while preserving all changes
                    new_status = "READY"
                    managed.status = "READY"
                    managed.updated_at = now_ms
                    actions_taken.append(
                        f"Worker {managed.id} refreshed from {prev_status} to READY (stale with changes preserved; changes untouched)"
                    )
                elif classification == WorkerClassification.ALREADY_INTEGRATED_UNCLOSED.value:
                    new_status = "INTEGRATED"
                    managed.status = "INTEGRATED"
                    if managed.integrated_at is None:
                        managed.integrated_at = now_ms
                    managed.updated_at = now_ms
                    actions_taken.append(
                        f"Worker {managed.id} marked INTEGRATED (changes integrated, preserved unclosed)"
                    )
                elif classification == WorkerClassification.READY.value:
                    new_status = "READY"
                    managed.status = "READY"
                    managed.updated_at = now_ms
                    actions_taken.append(
                        f"Worker {managed.id} refreshed from {prev_status} to READY (idle quiescent worker)"
                    )

            # 3. If missing worktree, do NOT delete. Record note.
            elif classification == WorkerClassification.MISSING_WORKTREE.value:
                actions_taken.append(
                    f"Worker {managed.id} has missing worktree ({managed.worktree_path}); record preserved without deletion"
                )

            # 4. If already integrated unclosed and status wasn't INTEGRATED
            elif (
                classification == WorkerClassification.ALREADY_INTEGRATED_UNCLOSED.value
                and prev_status != "INTEGRATED"
            ):
                new_status = "INTEGRATED"
                managed.status = "INTEGRATED"
                if managed.integrated_at is None:
                    managed.integrated_at = now_ms
                managed.updated_at = now_ms
                actions_taken.append(
                    f"Worker {managed.id} marked INTEGRATED (changes already in base)"
                )

            if new_status != prev_status:
                refreshed_workers.append(
                    {
                        "worker_id": managed.id,
                        "name": managed.name,
                        "previous_status": prev_status,
                        "new_status": new_status,
                        "classification": classification,
                    }
                )

        await db.commit()

    # Re-read fresh health report
    updated_health = await classify_workspace_health(
        workspace=workspace,
        user_id=user_id,
        identity=identity,
        stale_threshold_seconds=stale_threshold_seconds,
        active_threshold_seconds=active_threshold_seconds,
    )

    # Note any orphan worktrees or duplicate registrations in actions_taken
    if updated_health.get("summary", {}).get("orphan_worktrees", 0) > 0:
        actions_taken.append(
            f"Detected {updated_health['summary']['orphan_worktrees']} orphan git worktree(s); preserved without deletion"
        )

    if updated_health.get("duplicate_registration", {}).get("duplicate"):
        actions_taken.append(
            f"Detected duplicate workspace registration ({updated_health['duplicate_registration']['resolved_path']}); preserved non-destructively"
        )

    return {
        "workspace_id": workspace.id,
        "reconciled_at": now_ms,
        "refreshed_workers": refreshed_workers,
        "actions_taken": actions_taken,
        "discarded": False,
        "deleted_worktrees_count": 0,
        "deleted_branches_count": 0,
        "health": updated_health,
    }
