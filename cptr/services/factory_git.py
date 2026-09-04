"""Restart-safe Git commit and push lifecycle for verified factory cycles."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import FactoryCommitIntent, FactoryCycle, FactoryRun
from cptr.utils import git as git_utils
from cptr.utils.db import get_session_factory
from cptr.utils.identity import ExecutionIdentity
from cptr.utils.workspace_fingerprint import snapshot_workspace


class FactoryGitError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PushAuthorization:
    approved: bool
    approval_id: str
    revision: str
    remote: str
    branch: str

    def __post_init__(self) -> None:
        for field_name in ("approval_id", "revision", "remote", "branch"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"push authorization {field_name} must not be blank")


class FactoryGitAdapter(Protocol):
    async def current_revision(self, root: str) -> str: ...

    async def workspace_fingerprint(self, root: str) -> str: ...

    async def change_manifest(self, root: str) -> list[dict[str, str]]: ...

    async def diff(self, root: str) -> dict[str, Any]: ...

    async def diff_check(self, root: str) -> dict[str, Any]: ...

    async def stage(self, root: str, paths: list[str]) -> None: ...

    async def commit(self, root: str, message: str) -> dict[str, str]: ...

    async def log(self, root: str, limit: int = 50) -> list[dict[str, Any]]: ...

    async def push(self, root: str, *, remote: str, branch: str) -> dict[str, Any]: ...


class CptrGitAdapter:
    """Use CPTR's existing bounded Git primitives; no shell command strings."""

    def __init__(self, *, identity: ExecutionIdentity | None = None) -> None:
        self._identity = identity

    async def current_revision(self, root: str) -> str:
        return await git_utils.current_revision(root, self._identity)

    async def workspace_fingerprint(self, root: str) -> str:
        snapshot = await snapshot_workspace(root, self._identity)
        return str(snapshot["fingerprint"])

    async def change_manifest(self, root: str) -> list[dict[str, str]]:
        return await git_utils.change_manifest(root, self._identity)

    async def diff(self, root: str) -> dict[str, Any]:
        return await git_utils.diff(root, untracked=True, identity=self._identity)

    async def diff_check(self, root: str) -> dict[str, Any]:
        return await git_utils.diff_check(root, self._identity)

    async def stage(self, root: str, paths: list[str]) -> None:
        await git_utils.stage(root, paths, self._identity)

    async def commit(self, root: str, message: str) -> dict[str, str]:
        return await git_utils.commit(root, message, self._identity)

    async def log(self, root: str, limit: int = 50) -> list[dict[str, Any]]:
        return await git_utils.log(root, limit=limit, identity=self._identity)

    async def push(self, root: str, *, remote: str, branch: str) -> dict[str, Any]:
        return await git_utils.push(
            root,
            force=False,
            set_upstream=True,
            remote=remote,
            branch=branch,
            identity=self._identity,
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_repository_key(value: str) -> str:
    raw = (value or ".").strip() or "."
    posix = PurePosixPath(raw.replace("\\", "/"))
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or ".." in posix.parts:
        raise FactoryGitError(
            "FACTORY_GIT_INVALID_REPOSITORY_KEY",
            "repository_key must be a safe workspace-relative identifier",
        )
    return posix.as_posix()


_SENSITIVE_PATH_PARTS = {"credentials", "secrets"}
_SENSITIVE_FILENAMES = {".env", "id_rsa", "id_ed25519"}
_SENSITIVE_SUFFIXES = {".key", ".p12", ".pfx"}


def _is_sensitive_path(path: PurePosixPath) -> bool:
    parts = tuple(part.lower() for part in path.parts)
    name = path.name.lower()
    return (
        any(part in _SENSITIVE_PATH_PARTS for part in parts)
        or name in _SENSITIVE_FILENAMES
        or name.startswith(".env.")
        or any(name.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)
    )


def _changed_paths(manifest: list[dict[str, str]]) -> list[str]:
    paths: set[str] = set()
    for item in manifest:
        for key in ("path", "old_path"):
            value = str(item.get(key) or "").strip().replace("\\", "/")
            if not value:
                continue
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts:
                raise FactoryGitError(
                    "FACTORY_GIT_UNSAFE_DIFF_PATH",
                    "Git change manifest contains an unsafe path",
                )
            if _is_sensitive_path(path):
                raise FactoryGitError(
                    "FACTORY_GIT_SENSITIVE_PATH",
                    "factory commit intent refuses credential-sensitive changed paths",
                )
            paths.add(path.as_posix())
    return sorted(paths)


def _diff_digest(manifest: list[dict[str, str]], diff_payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"manifest": manifest, "diff": diff_payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class FactoryGitService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker | None = None,
        git: FactoryGitAdapter | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._git = git or CptrGitAdapter()

    async def prepare_commit_intent(
        self,
        *,
        run_id: str,
        cycle_id: str,
        repo_root: str,
        repository_key: str,
        message: str,
    ) -> FactoryCommitIntent:
        message = message.strip()
        if not message or len(message) > 400:
            raise ValueError("factory commit message must contain 1-400 characters")
        repository_key = _safe_repository_key(repository_key)
        run, cycle = await self._run_cycle(run_id, cycle_id)
        if not cycle.target_revision or not cycle.target_fingerprint:
            raise FactoryGitError(
                "FACTORY_GIT_MISSING_VERIFIED_TARGET",
                "factory cycle has no machine-verified target revision/fingerprint",
            )
        current_revision = await self._git.current_revision(repo_root)
        if current_revision != cycle.target_revision:
            raise FactoryGitError(
                "FACTORY_GIT_STALE_VERIFIED_REVISION",
                "repository revision changed after verification",
            )
        current_fingerprint = await self._git.workspace_fingerprint(repo_root)
        if current_fingerprint != cycle.target_fingerprint:
            raise FactoryGitError(
                "FACTORY_GIT_STALE_VERIFIED_FINGERPRINT",
                "workspace content changed after verification",
            )
        check = await self._git.diff_check(repo_root)
        if not check.get("passed"):
            raise FactoryGitError(
                "FACTORY_GIT_DIFF_CHECK_FAILED",
                "git diff --check failed for the verified target",
            )
        manifest = await self._git.change_manifest(repo_root)
        paths = _changed_paths(manifest)
        if not paths:
            raise FactoryGitError(
                "FACTORY_GIT_EMPTY_DIFF", "verified factory cycle has no Git changes"
            )
        diff_payload = await self._git.diff(repo_root)
        digest = _diff_digest(manifest, diff_payload)
        commit_message = f"{message} [factory:{cycle.id}:{digest[:12]}]"
        now = _now_ms()

        async with self._session_factory() as db:
            async with db.begin():
                existing = (
                    await db.execute(
                        select(FactoryCommitIntent).where(FactoryCommitIntent.cycle_id == cycle_id)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    immutable = (
                        existing.run_id,
                        existing.repository_key,
                        existing.verified_revision,
                        existing.verified_fingerprint,
                        existing.diff_digest,
                        list(existing.changed_paths or []),
                        existing.commit_message,
                    )
                    requested = (
                        run.id,
                        repository_key,
                        cycle.target_revision,
                        cycle.target_fingerprint,
                        digest,
                        paths,
                        commit_message,
                    )
                    if immutable != requested:
                        raise FactoryGitError(
                            "FACTORY_GIT_INTENT_CONFLICT",
                            "existing cycle commit intent differs from the current verified diff",
                        )
                    return existing
                intent = FactoryCommitIntent(
                    run_id=run.id,
                    cycle_id=cycle.id,
                    repository_key=repository_key,
                    verified_revision=cycle.target_revision,
                    verified_fingerprint=cycle.target_fingerprint,
                    diff_digest=digest,
                    changed_paths=paths,
                    commit_message=commit_message,
                    status="PREPARED",
                    created_at=now,
                    updated_at=now,
                )
                db.add(intent)
            return intent

    async def commit_intent(self, intent_id: str, *, repo_root: str) -> FactoryCommitIntent:
        intent = await self._intent(intent_id)
        if intent.status == "COMMITTED" and intent.commit_sha:
            return intent

        for item in await self._git.log(repo_root, limit=50):
            if str(item.get("message") or "") == intent.commit_message:
                commit_sha = str(item.get("hash") or "").strip()
                if commit_sha:
                    return await self._mark_committed(intent.id, commit_sha)

        current_revision = await self._git.current_revision(repo_root)
        if current_revision != intent.verified_revision:
            raise FactoryGitError(
                "FACTORY_GIT_STALE_VERIFIED_REVISION",
                "repository revision changed before the prepared commit completed",
            )
        current_fingerprint = await self._git.workspace_fingerprint(repo_root)
        if current_fingerprint != intent.verified_fingerprint:
            raise FactoryGitError(
                "FACTORY_GIT_STALE_VERIFIED_FINGERPRINT",
                "workspace content changed before the prepared commit completed",
            )
        check = await self._git.diff_check(repo_root)
        if not check.get("passed"):
            raise FactoryGitError(
                "FACTORY_GIT_DIFF_CHECK_FAILED",
                "git diff --check failed before commit",
            )
        manifest = await self._git.change_manifest(repo_root)
        paths = _changed_paths(manifest)
        digest = _diff_digest(manifest, await self._git.diff(repo_root))
        if paths != list(intent.changed_paths or []) or digest != intent.diff_digest:
            raise FactoryGitError(
                "FACTORY_GIT_STALE_VERIFIED_DIFF",
                "prepared commit diff changed after review",
            )
        await self._git.stage(repo_root, paths)
        await self._git.commit(repo_root, intent.commit_message)
        commit_sha = await self._git.current_revision(repo_root)
        return await self._mark_committed(intent.id, commit_sha)

    async def push_commit(
        self,
        intent_id: str,
        *,
        repo_root: str,
        authorization: PushAuthorization,
    ) -> FactoryCommitIntent:
        intent = await self._intent(intent_id)
        if intent.status != "COMMITTED" or not intent.commit_sha:
            raise FactoryGitError(
                "FACTORY_GIT_COMMIT_REQUIRED",
                "factory push requires a completed commit intent",
            )
        if not authorization.approved:
            raise FactoryGitError(
                "FACTORY_GIT_PUSH_APPROVAL_REQUIRED",
                "push policy requires explicit approval",
            )
        if authorization.revision != intent.commit_sha:
            raise FactoryGitError(
                "FACTORY_GIT_STALE_PUSH_APPROVAL",
                "push approval does not authorize the exact committed revision",
            )
        if intent.push_status == "PUSHED":
            if (
                intent.push_remote == authorization.remote
                and intent.push_branch == authorization.branch
                and intent.push_approval_id == authorization.approval_id
            ):
                return intent
            raise FactoryGitError(
                "FACTORY_GIT_PUSH_INTENT_CONFLICT",
                "commit was already pushed under a different authorization envelope",
            )
        current_revision = await self._git.current_revision(repo_root)
        if current_revision != intent.commit_sha:
            raise FactoryGitError(
                "FACTORY_GIT_STALE_PUSH_REVISION",
                "repository HEAD no longer matches the approved commit",
            )
        result = await self._git.push(
            repo_root,
            remote=authorization.remote,
            branch=authorization.branch,
        )
        if not result.get("ok"):
            raise FactoryGitError(
                "FACTORY_GIT_PUSH_FAILED",
                "Git provider rejected the approved push",
            )
        now = _now_ms()
        async with self._session_factory() as db:
            async with db.begin():
                row = await db.get(FactoryCommitIntent, intent.id)
                if row is None:
                    raise KeyError("factory commit intent not found")
                row.push_status = "PUSHED"
                row.push_remote = authorization.remote
                row.push_branch = authorization.branch
                row.push_approval_id = authorization.approval_id
                row.pushed_at = now
                row.updated_at = now
            return row

    async def get_intent_for_cycle(self, cycle_id: str) -> FactoryCommitIntent:
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(FactoryCommitIntent).where(FactoryCommitIntent.cycle_id == cycle_id)
                )
            ).scalar_one_or_none()
            if row is None:
                raise FactoryGitError(
                    "FACTORY_GIT_INTENT_NOT_FOUND",
                    "factory cycle has no prepared commit intent",
                )
            return row

    async def _run_cycle(self, run_id: str, cycle_id: str) -> tuple[FactoryRun, FactoryCycle]:
        async with self._session_factory() as db:
            run = await db.get(FactoryRun, run_id)
            cycle = await db.get(FactoryCycle, cycle_id)
            if run is None or cycle is None or cycle.run_id != run_id:
                raise KeyError("factory run/cycle not found")
            if run.current_cycle_id != cycle_id:
                raise FactoryGitError(
                    "FACTORY_GIT_STALE_CYCLE",
                    "Git lifecycle requires the current factory cycle",
                )
            return run, cycle

    async def _intent(self, intent_id: str) -> FactoryCommitIntent:
        async with self._session_factory() as db:
            row = await db.get(FactoryCommitIntent, intent_id)
            if row is None:
                raise KeyError("factory commit intent not found")
            return row

    async def _mark_committed(self, intent_id: str, commit_sha: str) -> FactoryCommitIntent:
        if not commit_sha.strip():
            raise FactoryGitError("FACTORY_GIT_INVALID_COMMIT", "commit SHA must not be blank")
        now = _now_ms()
        async with self._session_factory() as db:
            async with db.begin():
                row = await db.get(FactoryCommitIntent, intent_id)
                if row is None:
                    raise KeyError("factory commit intent not found")
                if row.status == "COMMITTED" and row.commit_sha and row.commit_sha != commit_sha:
                    raise FactoryGitError(
                        "FACTORY_GIT_COMMIT_CONFLICT",
                        "prepared intent resolved to a different commit SHA",
                    )
                row.status = "COMMITTED"
                row.commit_sha = commit_sha
                row.committed_at = row.committed_at or now
                row.updated_at = now
            return row
