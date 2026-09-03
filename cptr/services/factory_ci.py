"""Single-observation, revision-bound CI tracking for Dark Factory cycles."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import FactoryCiRun, FactoryCycle, FactoryRun
from cptr.utils.db import get_session_factory
from cptr.utils.redaction import redact_text


class FactoryCiError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CiObservation:
    status: str
    conclusion: str | None = None
    url: str | None = None
    failure_summary: str | None = None

    def __post_init__(self) -> None:
        if not self.status.strip():
            raise ValueError("CI observation status must not be blank")
        if self.url is not None and len(self.url) > 4_096:
            raise ValueError("CI observation URL exceeds bounded length")
        if self.failure_summary is not None and len(self.failure_summary) > 8_000:
            raise ValueError("CI failure summary exceeds bounded length")


@dataclass(frozen=True)
class CiPollRequest:
    provider: str
    repository: str
    revision: str
    external_run_id: str
    check_id: str


class CiProvider(Protocol):
    async def observe(self, request: CiPollRequest) -> CiObservation: ...


@dataclass(frozen=True)
class CiRunIdentity:
    external_run_id: str
    workflow: str
    url: str | None = None


_GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GIT_REVISION_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_GH_OUTPUT_LIMIT = 512 * 1024
GhCommandRunner = Callable[[tuple[str, ...], float], Awaitable[tuple[int, str, str]]]


async def _run_gh_command(argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise FactoryCiError(
            "FACTORY_CI_PROVIDER_TIMEOUT", "GitHub Actions observation timed out"
        ) from exc
    if len(stdout) > _GH_OUTPUT_LIMIT or len(stderr) > _GH_OUTPUT_LIMIT:
        raise FactoryCiError(
            "FACTORY_CI_PROVIDER_OUTPUT_TOO_LARGE",
            "GitHub Actions response exceeded the bounded limit",
        )
    return (
        int(process.returncode or 0),
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


class GitHubActionsCliProvider:
    """Read exact-revision GitHub Actions state through bounded ``gh`` argv calls."""

    provider_name = "github"

    def __init__(
        self,
        *,
        executable: str | None = None,
        timeout_seconds: float = 20.0,
        command_runner: GhCommandRunner | None = None,
    ) -> None:
        self._executable = (executable or os.environ.get("CPTR_GH_EXECUTABLE") or "gh").strip()
        self._timeout_seconds = float(timeout_seconds)
        self._run = command_runner or _run_gh_command
        if not self._executable:
            raise ValueError("GitHub CLI executable must not be blank")
        if self._timeout_seconds <= 0 or self._timeout_seconds > 60:
            raise ValueError("GitHub CI provider timeout must be between 0 and 60 seconds")

    @staticmethod
    def _repository(value: str) -> str:
        repository = value.strip()
        if not _GITHUB_REPOSITORY_RE.fullmatch(repository):
            raise FactoryCiError(
                "FACTORY_CI_INVALID_REPOSITORY", "GitHub repository must use owner/name syntax"
            )
        return repository

    @staticmethod
    def _revision(value: str) -> str:
        revision = value.strip()
        if not _GIT_REVISION_RE.fullmatch(revision):
            raise FactoryCiError(
                "FACTORY_CI_INVALID_REVISION", "GitHub CI requires an immutable Git revision"
            )
        return revision.lower()

    async def _json(self, argv: tuple[str, ...]) -> object:
        try:
            code, stdout, _stderr = await self._run(argv, self._timeout_seconds)
        except OSError as exc:
            raise FactoryCiError(
                "FACTORY_CI_PROVIDER_UNAVAILABLE", "GitHub CLI is unavailable"
            ) from exc
        if code != 0:
            raise FactoryCiError("FACTORY_CI_PROVIDER_FAILURE", "GitHub Actions observation failed")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise FactoryCiError(
                "FACTORY_CI_PROVIDER_INVALID_RESPONSE", "GitHub Actions returned invalid JSON"
            ) from exc

    async def discover(self, *, repository: str, revision: str) -> tuple[CiRunIdentity, ...]:
        repository = self._repository(repository)
        revision = self._revision(revision)
        payload = await self._json(
            (
                self._executable,
                "run",
                "list",
                "--repo",
                repository,
                "--commit",
                revision,
                "--limit",
                "50",
                "--json",
                "databaseId,headSha,status,conclusion,url,name,workflowName",
            )
        )
        if not isinstance(payload, list):
            raise FactoryCiError(
                "FACTORY_CI_PROVIDER_INVALID_RESPONSE", "GitHub Actions run list must be an array"
            )
        rows: list[CiRunIdentity] = []
        for item in payload[:50]:
            if not isinstance(item, dict):
                continue
            head_sha = str(item.get("headSha") or "").strip().lower()
            if head_sha != revision:
                continue
            run_id = str(item.get("databaseId") or "").strip()
            workflow = str(item.get("workflowName") or item.get("name") or "").strip()
            if not run_id or not workflow:
                continue
            rows.append(
                CiRunIdentity(
                    external_run_id=run_id[:120],
                    workflow=workflow[:500],
                    url=(str(item.get("url") or "").strip() or None),
                )
            )
        rows.sort(
            key=lambda row: int(row.external_run_id) if row.external_run_id.isdigit() else -1,
            reverse=True,
        )
        return tuple(rows)

    async def observe(self, request: CiPollRequest) -> CiObservation:
        repository = self._repository(request.repository)
        revision = self._revision(request.revision)
        run_id = _token(request.external_run_id, "GitHub Actions run ID", 120)
        if not run_id.isdigit():
            raise FactoryCiError(
                "FACTORY_CI_INVALID_RUN_ID", "GitHub Actions run ID must be numeric"
            )
        payload = await self._json(
            (
                self._executable,
                "run",
                "view",
                run_id,
                "--repo",
                repository,
                "--json",
                "databaseId,headSha,status,conclusion,url,name,workflowName",
            )
        )
        if not isinstance(payload, dict):
            raise FactoryCiError(
                "FACTORY_CI_PROVIDER_INVALID_RESPONSE", "GitHub Actions run must be an object"
            )
        if str(payload.get("headSha") or "").strip().lower() != revision:
            raise FactoryCiError(
                "FACTORY_CI_STALE_REVISION",
                "GitHub Actions run does not match the tracked revision",
            )
        workflow = str(payload.get("workflowName") or payload.get("name") or "").strip()
        if request.check_id and workflow != request.check_id:
            raise FactoryCiError(
                "FACTORY_CI_WORKFLOW_MISMATCH",
                "GitHub Actions run does not match the tracked workflow",
            )
        status = str(payload.get("status") or "").strip()
        conclusion = str(payload.get("conclusion") or "").strip() or None
        failure_summary = None
        if conclusion and _conclusion(conclusion) in _FAILURE_CONCLUSIONS:
            failure_summary = (
                f"GitHub Actions workflow {workflow or request.check_id} concluded {conclusion}"
            )
        return CiObservation(
            status=status,
            conclusion=conclusion,
            url=(str(payload.get("url") or "").strip() or None),
            failure_summary=failure_summary,
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _token(value: str, label: str, max_length: int = 500) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be blank")
    if len(normalized) > max_length:
        raise ValueError(f"{label} exceeds bounded length")
    return normalized


def _status(value: str) -> str:
    normalized = value.strip().upper().replace("-", "_")
    aliases = {
        "IN_PROGRESS": "IN_PROGRESS",
        "RUNNING": "IN_PROGRESS",
        "QUEUED": "QUEUED",
        "PENDING": "QUEUED",
        "REQUESTED": "QUEUED",
        "WAITING": "QUEUED",
        "COMPLETED": "COMPLETED",
        "COMPLETE": "COMPLETED",
    }
    if normalized not in aliases:
        raise FactoryCiError("FACTORY_CI_INVALID_STATUS", f"unsupported CI status {value!r}")
    return aliases[normalized]


def _conclusion(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().upper().replace("-", "_")
    aliases = {
        "SUCCESS": "SUCCESS",
        "PASSED": "SUCCESS",
        "FAILURE": "FAILURE",
        "FAILED": "FAILURE",
        "CANCELLED": "CANCELLED",
        "CANCELED": "CANCELLED",
        "TIMED_OUT": "TIMED_OUT",
        "TIMEOUT": "TIMED_OUT",
        "SKIPPED": "SKIPPED",
        "NEUTRAL": "NEUTRAL",
        "ACTION_REQUIRED": "ACTION_REQUIRED",
        "STALE": "FAILURE",
        "STARTUP_FAILURE": "FAILURE",
    }
    if normalized not in aliases:
        raise FactoryCiError(
            "FACTORY_CI_INVALID_CONCLUSION", f"unsupported CI conclusion {value!r}"
        )
    return aliases[normalized]


_FAILURE_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}


class FactoryCiService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker | None = None,
        providers: Mapping[str, CiProvider],
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        normalized = {}
        for name, provider in providers.items():
            key = _token(str(name), "CI provider", 120).lower()
            if key in normalized:
                raise ValueError(f"duplicate CI provider {key}")
            normalized[key] = provider
        self._providers = normalized

    async def begin_tracking(
        self,
        *,
        run_id: str,
        cycle_id: str,
        provider: str,
        repository: str,
        revision: str,
        external_run_id: str,
        check_id: str | None = None,
        url: str | None = None,
    ) -> FactoryCiRun:
        provider = _token(provider, "CI provider", 120).lower()
        repository = _token(repository, "CI repository")
        revision = _token(revision, "CI revision")
        external_run_id = _token(external_run_id, "CI run ID")
        check_id = _token(check_id, "CI check ID") if check_id is not None else ""
        if provider not in self._providers:
            raise FactoryCiError(
                "FACTORY_CI_PROVIDER_UNAVAILABLE",
                f"CI provider {provider} is not configured",
            )
        now = _now_ms()
        async with self._session_factory() as db:
            async with db.begin():
                run = await db.get(FactoryRun, run_id)
                cycle = await db.get(FactoryCycle, cycle_id)
                if run is None or cycle is None or cycle.run_id != run_id:
                    raise KeyError("factory run/cycle not found")
                if run.current_cycle_id != cycle_id:
                    raise FactoryCiError(
                        "FACTORY_CI_STALE_CYCLE", "CI tracking requires the current factory cycle"
                    )
                exact = (
                    await db.execute(
                        select(FactoryCiRun).where(
                            FactoryCiRun.provider == provider,
                            FactoryCiRun.repository == repository,
                            FactoryCiRun.external_run_id == external_run_id,
                            FactoryCiRun.check_id == check_id,
                        )
                    )
                ).scalar_one_or_none()
                if exact is not None:
                    if (
                        exact.run_id != run_id
                        or exact.cycle_id != cycle_id
                        or exact.revision != revision
                    ):
                        raise FactoryCiError(
                            "FACTORY_CI_TRACKING_CONFLICT",
                            "existing CI identity is bound to a different factory target",
                        )
                    return exact
                pending_diagnosis = (
                    (
                        await db.execute(
                            select(FactoryCiRun).where(
                                FactoryCiRun.cycle_id == cycle_id,
                                FactoryCiRun.provider == provider,
                                FactoryCiRun.revision == revision,
                                FactoryCiRun.diagnosis_required.is_(True),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if pending_diagnosis is not None:
                    raise FactoryCiError(
                        "FACTORY_CI_DIAGNOSIS_REQUIRED",
                        "failed CI must be diagnosed before another run is tracked for this revision",
                    )
                row = FactoryCiRun(
                    run_id=run_id,
                    cycle_id=cycle_id,
                    provider=provider,
                    repository=repository,
                    revision=revision,
                    external_run_id=external_run_id,
                    check_id=check_id,
                    status="QUEUED",
                    conclusion=None,
                    url=redact_text(url)[:4_096] if url else None,
                    diagnosis_required=False,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
            return row

    async def poll_once(self, ci_run_id: str) -> FactoryCiRun:
        row = await self._get(ci_run_id)
        provider = self._providers.get(row.provider)
        if provider is None:
            raise FactoryCiError(
                "FACTORY_CI_PROVIDER_UNAVAILABLE",
                f"CI provider {row.provider} is not configured",
            )
        # Exactly one provider observation per call. Scheduling/retries happen at
        # the durable orchestrator level; no sleep loop is hidden in this method.
        observation = await provider.observe(
            CiPollRequest(
                provider=row.provider,
                repository=row.repository,
                revision=row.revision,
                external_run_id=row.external_run_id,
                check_id=row.check_id,
            )
        )
        status = _status(observation.status)
        conclusion = _conclusion(observation.conclusion)
        if status != "COMPLETED" and conclusion is not None:
            raise FactoryCiError(
                "FACTORY_CI_INVALID_OBSERVATION",
                "CI conclusion cannot be terminal while status is non-terminal",
            )
        async with self._session_factory() as db:
            async with db.begin():
                current = await db.get(FactoryCiRun, ci_run_id)
                if current is None:
                    raise KeyError("factory CI run not found")
                latest_observed = (
                    await db.execute(
                        select(func.max(FactoryCiRun.last_observed_at)).where(
                            FactoryCiRun.cycle_id == current.cycle_id,
                            FactoryCiRun.provider == current.provider,
                            FactoryCiRun.revision == current.revision,
                        )
                    )
                ).scalar_one()
                now = max(_now_ms(), int(latest_observed or 0) + 1)
                current.status = status
                current.conclusion = conclusion
                if observation.url:
                    current.url = redact_text(observation.url)[:4_096]
                if observation.failure_summary:
                    current.failure_summary = redact_text(observation.failure_summary)[:8_000]
                current.diagnosis_required = bool(conclusion in _FAILURE_CONCLUSIONS)
                current.last_observed_at = now
                current.updated_at = now
            return current

    async def record_diagnosis(self, ci_run_id: str, summary: str) -> FactoryCiRun:
        summary = summary.strip()
        if not summary:
            raise ValueError("CI diagnosis summary must not be blank")
        now = _now_ms()
        async with self._session_factory() as db:
            async with db.begin():
                row = await db.get(FactoryCiRun, ci_run_id)
                if row is None:
                    raise KeyError("factory CI run not found")
                if row.conclusion not in _FAILURE_CONCLUSIONS:
                    raise FactoryCiError(
                        "FACTORY_CI_DIAGNOSIS_NOT_REQUIRED",
                        "only failed CI observations require rerun diagnosis",
                    )
                row.diagnosis_summary = redact_text(summary)[:8_000]
                row.diagnosis_required = False
                row.diagnosed_at = now
                row.updated_at = now
            return row

    async def has_current_pass(self, cycle_id: str, revision: str) -> bool:
        revision = _token(revision, "CI revision")
        async with self._session_factory() as db:
            latest = (
                await db.execute(
                    select(FactoryCiRun)
                    .where(
                        FactoryCiRun.cycle_id == cycle_id,
                        FactoryCiRun.revision == revision,
                        FactoryCiRun.last_observed_at.is_not(None),
                    )
                    .order_by(
                        FactoryCiRun.last_observed_at.desc(),
                        FactoryCiRun.updated_at.desc(),
                        FactoryCiRun.id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return bool(
                latest is not None
                and latest.status == "COMPLETED"
                and latest.conclusion == "SUCCESS"
                and not latest.diagnosis_required
            )

    async def _get(self, ci_run_id: str) -> FactoryCiRun:
        async with self._session_factory() as db:
            row = await db.get(FactoryCiRun, ci_run_id)
            if row is None:
                raise KeyError("factory CI run not found")
            return row
