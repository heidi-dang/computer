"""Versioned, isolated coding benchmark with server-owned randomized grading."""

from __future__ import annotations

import secrets
import shutil
import statistics
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import select

from cptr.env import DATA_DIR
from cptr.models import CodingBenchmarkRun, Workspace
from cptr.services.coding_benchmark_grader import grade_workspace
from cptr.services.mcp_pricing import normalize_pricing_model
from cptr.utils.db import get_db
from cptr.utils.redaction import redact_external_text

SUITE_ID = "cptr-python-core"
SUITE_VERSION = "1"
MAX_SCORE = 100
GRADER_TIMEOUT_SECONDS = 15

_TASKS = [
    {
        "id": "interval_merge",
        "file": "intervals.py",
        "points": 30,
        "instruction": (
            "Implement merge_intervals(intervals). Each item is a two-value iterable. Normalize reversed "
            "endpoints, sort the intervals, merge overlaps or touching endpoints, and return a list of "
            "(start, end) tuples. Do not mutate the caller's input."
        ),
    },
    {
        "id": "ttl_lru_cache",
        "file": "ttl_cache.py",
        "points": 35,
        "instruction": (
            "Implement TTLCache(capacity, ttl_seconds) with set(key, value, now), get(key, now, default=None), "
            "and delete(key). capacity and ttl_seconds must be positive. Expired entries are absent, get refreshes "
            "LRU recency without extending TTL, set replaces and refreshes TTL, and insertion evicts the least "
            "recently used live entry when capacity is exceeded."
        ),
    },
    {
        "id": "retry_policy",
        "file": "retry_policy.py",
        "points": 35,
        "instruction": (
            "Implement retry_call(fn, *, attempts, retryable). attempts must be >= 1. Call fn until it succeeds; "
            "retry only exceptions matching retryable, propagate non-retryable exceptions immediately, and raise "
            "the last retryable exception after the final allowed attempt. Return fn's successful value unchanged."
        ),
    },
]

_STARTERS = {
    "intervals.py": '''"""Standardized benchmark task: interval merge."""\n\ndef merge_intervals(intervals):\n    raise NotImplementedError\n''',
    "ttl_cache.py": '''"""Standardized benchmark task: bounded TTL/LRU cache."""\n\nclass TTLCache:\n    def __init__(self, capacity, ttl_seconds):\n        raise NotImplementedError\n\n    def set(self, key, value, now):\n        raise NotImplementedError\n\n    def get(self, key, now, default=None):\n        raise NotImplementedError\n\n    def delete(self, key):\n        raise NotImplementedError\n''',
    "retry_policy.py": '''"""Standardized benchmark task: retry policy."""\n\ndef retry_call(fn, *, attempts, retryable):\n    raise NotImplementedError\n''',
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _benchmark_model(model_reported: str | None) -> str | None:
    canonical = normalize_pricing_model(model_reported, None)
    if canonical:
        return canonical
    if not model_reported or not model_reported.strip():
        return None
    return "-".join(model_reported.strip().lower().replace("_", " ").split())[:120]


def _readme() -> str:
    lines = [
        "# CPTR Standardized Coding Benchmark",
        "",
        f"Suite: `{SUITE_ID}` version `{SUITE_VERSION}` · 100 points total.",
        "",
        "Implement the three starter files. Hidden randomized grading is server-owned and not stored in this workspace.",
        "Do not add dependencies or use network access. Only the documented public behavior is graded.",
        "",
    ]
    for index, task in enumerate(_TASKS, start=1):
        lines.extend(
            [
                f"## {index}. {task['id']} — {task['points']} points",
                "",
                f"File: `{task['file']}`",
                "",
                str(task["instruction"]),
                "",
            ]
        )
    return "\n".join(lines)


class CodingBenchmarkStore:
    def __init__(self, *, session_factory=None, data_dir: Path | None = None) -> None:
        self._session_factory = session_factory
        self.data_dir = Path(data_dir) if data_dir is not None else DATA_DIR

    @asynccontextmanager
    async def _session(self):
        if self._session_factory is not None:
            async with self._session_factory() as db:
                yield db
            return
        async with await get_db() as db:
            yield db

    async def start(
        self, owner_id: str, *, model_reported: str | None, suite_id: str = SUITE_ID
    ) -> dict[str, object]:
        if suite_id != SUITE_ID:
            raise ValueError("unknown benchmark suite")
        run_id = f"bench_{uuid.uuid4().hex}"
        workspace_id = str(uuid.uuid4())
        root = (self.data_dir / "benchmarks" / run_id).resolve()
        root.mkdir(parents=True, exist_ok=False)
        try:
            (root / "README.md").write_text(_readme(), encoding="utf-8")
            for name, content in _STARTERS.items():
                (root / name).write_text(content, encoding="utf-8")
            now = _now_ms()
            async with self._session() as db:
                db.add(
                    Workspace(
                        id=workspace_id,
                        user_id=owner_id,
                        path=str(root),
                        name=f"Benchmark · {SUITE_ID} v{SUITE_VERSION}",
                        data={"benchmark_run_id": run_id, "temporary": True},
                        created_at=now // 1000,
                        updated_at=now // 1000,
                    )
                )
                run = CodingBenchmarkRun(
                    id=run_id,
                    user_id=owner_id,
                    suite_id=SUITE_ID,
                    suite_version=SUITE_VERSION,
                    model_reported=model_reported,
                    model_canonical=_benchmark_model(model_reported),
                    status="READY",
                    workspace_id=workspace_id,
                    workspace_path=str(root),
                    grader_seed=secrets.token_hex(16),
                    score=None,
                    max_score=MAX_SCORE,
                    case_results=[],
                    started_at_ms=now,
                )
                db.add(run)
                await db.commit()
                return self._public_run(run, include_seed=False)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

    async def workspace_path(self, owner_id: str, run_id: str) -> str | None:
        async with self._session() as db:
            run = await db.scalar(
                select(CodingBenchmarkRun).where(
                    CodingBenchmarkRun.id == run_id,
                    CodingBenchmarkRun.user_id == owner_id,
                )
            )
            return str(run.workspace_path) if run else None

    async def get(self, owner_id: str, run_id: str) -> dict[str, object] | None:
        async with self._session() as db:
            run = await db.scalar(
                select(CodingBenchmarkRun).where(
                    CodingBenchmarkRun.id == run_id,
                    CodingBenchmarkRun.user_id == owner_id,
                )
            )
            return self._public_run(run, include_seed=run.status != "READY") if run else None

    async def submit(self, owner_id: str, run_id: str) -> dict[str, object]:
        async with self._session() as db:
            run = await db.scalar(
                select(CodingBenchmarkRun).where(
                    CodingBenchmarkRun.id == run_id,
                    CodingBenchmarkRun.user_id == owner_id,
                )
            )
            if run is None:
                raise KeyError("benchmark run not found")
            if run.status != "READY":
                return self._public_run(run, include_seed=True)
            root = Path(run.workspace_path)
            if not root.is_dir():
                run.status = "FAILED"
                run.score = 0
                run.case_results = []
                run.error_summary = "benchmark workspace is unavailable"
                run.completed_at_ms = _now_ms()
                run.duration_ms = run.completed_at_ms - int(run.started_at_ms)
                await db.commit()
                return self._public_run(run, include_seed=True)
            try:
                grade = await self._grade(root, run.grader_seed)
                run.status = "COMPLETE"
                run.score = int(grade["score"])
                run.case_results = list(grade["case_results"])
            except TimeoutError:
                run.status = "FAILED"
                run.score = 0
                run.case_results = []
                run.error_summary = "benchmark grader timed out"
            except Exception as exc:
                run.status = "FAILED"
                run.score = 0
                run.case_results = []
                run.error_summary = (
                    redact_external_text(str(exc)).strip()[:500] or "benchmark grader failed"
                )
            run.completed_at_ms = _now_ms()
            run.duration_ms = run.completed_at_ms - int(run.started_at_ms)
            await db.commit()
            return self._public_run(run, include_seed=True)

    async def _grade(self, root: Path, seed: str) -> dict[str, object]:
        return await grade_workspace(root, seed, timeout_seconds=GRADER_TIMEOUT_SECONDS)

    async def leaderboard(self, owner_id: str, *, suite_id: str = SUITE_ID) -> dict[str, object]:
        if suite_id != SUITE_ID:
            raise ValueError("unknown benchmark suite")
        async with self._session() as db:
            rows = list(
                (
                    await db.scalars(
                        select(CodingBenchmarkRun).where(
                            CodingBenchmarkRun.user_id == owner_id,
                            CodingBenchmarkRun.suite_id == SUITE_ID,
                            CodingBenchmarkRun.suite_version == SUITE_VERSION,
                            CodingBenchmarkRun.status == "COMPLETE",
                        )
                    )
                ).all()
            )
        grouped: dict[str, list[CodingBenchmarkRun]] = {}
        for row in rows:
            key = row.model_canonical or row.model_reported or "unreported"
            grouped.setdefault(key, []).append(row)
        models = []
        for key, attempts in grouped.items():
            scores = [int(item.score or 0) for item in attempts]
            durations = [int(item.duration_ms or 0) for item in attempts]
            models.append(
                {
                    "model_canonical": key,
                    "model_reported": attempts[-1].model_reported,
                    "attempts": len(attempts),
                    "best_score": max(scores),
                    "average_score": round(sum(scores) / len(scores), 2),
                    "perfect_runs": sum(1 for score in scores if score == MAX_SCORE),
                    "pass_rate": round(
                        sum(1 for score in scores if score == MAX_SCORE) / len(scores), 4
                    ),
                    "median_duration_ms": int(statistics.median(durations)),
                }
            )
        models.sort(
            key=lambda item: (
                -item["best_score"],
                -item["average_score"],
                item["median_duration_ms"],
                item["model_canonical"],
            )
        )
        return {
            "comparable": True,
            "comparability": "standardized_suite_only",
            "suite_id": SUITE_ID,
            "suite_version": SUITE_VERSION,
            "max_score": MAX_SCORE,
            "models": models,
        }

    def _public_run(self, run: CodingBenchmarkRun, *, include_seed: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "run_id": run.id,
            "suite_id": run.suite_id,
            "suite_version": run.suite_version,
            "status": run.status,
            "model_reported": run.model_reported,
            "model_canonical": run.model_canonical,
            "workspace_id": run.workspace_id,
            "score": int(run.score) if run.score is not None else None,
            "max_score": int(run.max_score or MAX_SCORE),
            "case_results": list(run.case_results or []),
            "error_summary": run.error_summary,
            "started_at_ms": int(run.started_at_ms),
            "completed_at_ms": int(run.completed_at_ms)
            if run.completed_at_ms is not None
            else None,
            "duration_ms": int(run.duration_ms) if run.duration_ms is not None else None,
            "comparable": True,
            "comparability": "standardized_suite_only",
            "tasks": [dict(task) for task in _TASKS],
        }
        if include_seed:
            payload["grader_seed"] = run.grader_seed
        return payload


coding_benchmark_store = CodingBenchmarkStore()
