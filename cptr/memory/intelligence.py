"""Structured procedural and failure intelligence derived from canonical memory."""

from __future__ import annotations

import hashlib
import re
import time
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import delete, select

from cptr.models import MemoryFailureProfile, MemoryProcedureProfile
from cptr.utils.db import get_session_factory
from cptr.utils.redaction import redact_text

_STEP_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$")
_LABEL_RE = re.compile(
    r"(?i)(symptoms?|root\s+cause|attempted\s+fix(?:es)?|successful\s+fix|verification)\s*:\s*"
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe(value: Any, limit: int = 2000) -> str:
    return " ".join(redact_text(str(value or "")).split())[:limit]


def procedure_profile(text: str) -> dict[str, Any]:
    safe = redact_text(text)
    lines = [line.strip() for line in safe.splitlines() if line.strip()]
    steps: list[str] = []
    for line in lines:
        match = _STEP_RE.match(line)
        if match:
            steps.append(_safe(match.group(1), 1000))
    if not steps and ";" in safe:
        steps = [_safe(item, 1000) for item in safe.split(";") if _safe(item, 1000)][:30]
    verification = [
        _safe(line, 1000)
        for line in lines
        if any(
            token in line.lower() for token in ("verify", "verification", "health", "smoke", "test")
        )
    ][:20]
    trigger = ""
    if lines:
        trigger = _safe(lines[0].split(":", 1)[0], 500)
    return {
        "trigger": trigger,
        "steps": list(dict.fromkeys(step for step in steps if step))[:50],
        "verification": list(dict.fromkeys(item for item in verification if item))[:20],
    }


def failure_profile(text: str) -> dict[str, Any]:
    safe = redact_text(text)
    matches = list(_LABEL_RE.finditer(safe))
    fields: dict[str, list[str]] = {}
    for index, match in enumerate(matches):
        label = " ".join(match.group(1).lower().split())
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(safe)
        value = _safe(safe[start:end].strip(" .;\n"), 2000)
        if value:
            fields.setdefault(label, []).append(value)
    symptoms = [*fields.get("symptom", []), *fields.get("symptoms", [])]
    attempted = [*fields.get("attempted fix", []), *fields.get("attempted fixes", [])]
    root_cause = (fields.get("root cause") or [""])[0]
    successful_fix = (fields.get("successful fix") or [""])[0]
    verification = (fields.get("verification") or [""])[0]
    if not symptoms:
        first_sentence = _safe(safe.split(".", 1)[0], 1000)
        symptoms = [first_sentence] if first_sentence else []
    signature_source = "|".join([*symptoms, root_cause, successful_fix]).lower()
    signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()[:24]
    return {
        "signature": signature,
        "symptoms": list(dict.fromkeys(symptoms))[:20],
        "root_cause": root_cause,
        "attempted_fixes": list(dict.fromkeys(attempted))[:20],
        "successful_fix": successful_fix,
        "verification": verification,
    }


class MemoryIntelligenceStore:
    def __init__(self, *, session_factory=None) -> None:
        self._session_factory = session_factory or get_session_factory()

    @asynccontextmanager
    async def _session(self):
        async with self._session_factory() as db:
            yield db

    async def clear_scope(self, *, user_id: str, workspace: str) -> None:
        workspace = str(workspace or "")
        async with self._session() as db:
            async with db.begin():
                await db.execute(
                    delete(MemoryProcedureProfile).where(
                        MemoryProcedureProfile.user_id == user_id,
                        MemoryProcedureProfile.workspace == workspace,
                    )
                )
                await db.execute(
                    delete(MemoryFailureProfile).where(
                        MemoryFailureProfile.user_id == user_id,
                        MemoryFailureProfile.workspace == workspace,
                    )
                )

    async def project(self, row: dict[str, Any]) -> None:
        kind = str(row.get("kind") or "")
        if kind == "procedure":
            await self._project_procedure(row)
        elif kind == "failure":
            await self._project_failure(row)

    async def _project_procedure(self, row: dict[str, Any]) -> None:
        memory_id = str(row.get("memory_id") or "")
        if not memory_id:
            return
        parsed = procedure_profile(str(row.get("canonical_text") or ""))
        structured = (
            row.get("structured_value") if isinstance(row.get("structured_value"), dict) else {}
        )
        steps = (
            structured.get("steps")
            if isinstance(structured.get("steps"), list)
            else parsed["steps"]
        )
        verification = (
            structured.get("verification")
            if isinstance(structured.get("verification"), list)
            else parsed["verification"]
        )
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                profile = await db.get(MemoryProcedureProfile, memory_id)
                if profile is None:
                    profile = MemoryProcedureProfile(
                        memory_id=memory_id,
                        user_id=str(row.get("user_id") or ""),
                        workspace=str(row.get("workspace") or ""),
                        trigger=_safe(structured.get("trigger") or parsed["trigger"], 500),
                        steps=[_safe(item, 1000) for item in steps if _safe(item, 1000)][:50],
                        verification=[
                            _safe(item, 1000) for item in verification if _safe(item, 1000)
                        ][:20],
                        success_count=0,
                        failure_count=0,
                        updated_at_ms=now,
                    )
                    db.add(profile)
                else:
                    profile.trigger = _safe(structured.get("trigger") or parsed["trigger"], 500)
                    profile.steps = [_safe(item, 1000) for item in steps if _safe(item, 1000)][:50]
                    profile.verification = [
                        _safe(item, 1000) for item in verification if _safe(item, 1000)
                    ][:20]
                    profile.updated_at_ms = now

    async def _project_failure(self, row: dict[str, Any]) -> None:
        memory_id = str(row.get("memory_id") or "")
        if not memory_id:
            return
        parsed = failure_profile(str(row.get("canonical_text") or ""))
        structured = (
            row.get("structured_value") if isinstance(row.get("structured_value"), dict) else {}
        )
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                profile = await db.get(MemoryFailureProfile, memory_id)
                if profile is None:
                    profile = MemoryFailureProfile(
                        memory_id=memory_id,
                        user_id=str(row.get("user_id") or ""),
                        workspace=str(row.get("workspace") or ""),
                        signature=_safe(structured.get("signature") or parsed["signature"], 500),
                        symptoms=structured.get("symptoms")
                        if isinstance(structured.get("symptoms"), list)
                        else parsed["symptoms"],
                        root_cause=_safe(structured.get("root_cause") or parsed["root_cause"]),
                        attempted_fixes=structured.get("attempted_fixes")
                        if isinstance(structured.get("attempted_fixes"), list)
                        else parsed["attempted_fixes"],
                        successful_fix=_safe(
                            structured.get("successful_fix") or parsed["successful_fix"]
                        ),
                        verification=_safe(
                            structured.get("verification") or parsed["verification"]
                        ),
                        recurrence_count=1,
                        last_seen_at_ms=now,
                        updated_at_ms=now,
                    )
                    db.add(profile)
                else:
                    profile.last_seen_at_ms = now
                    profile.updated_at_ms = now

    async def record_outcome(self, memory_id: str, *, outcome: str) -> None:
        normalized = str(outcome or "").strip().lower()
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                procedure = await db.get(MemoryProcedureProfile, memory_id)
                if procedure is not None:
                    if normalized in {"success", "helpful", "passed", "complete"}:
                        procedure.success_count = int(procedure.success_count or 0) + 1
                    elif normalized in {"failure", "failed", "unhelpful", "error"}:
                        procedure.failure_count = int(procedure.failure_count or 0) + 1
                    procedure.last_outcome_at_ms = now
                    procedure.updated_at_ms = now
                failure = await db.get(MemoryFailureProfile, memory_id)
                if failure is not None:
                    failure.last_seen_at_ms = now
                    failure.updated_at_ms = now

    async def get_procedure(self, memory_id: str) -> dict[str, Any]:
        async with self._session() as db:
            row = await db.get(MemoryProcedureProfile, memory_id)
            if row is None:
                raise KeyError("procedure profile not found")
            return {
                "memory_id": row.memory_id,
                "trigger": row.trigger,
                "steps": list(row.steps or []),
                "verification": list(row.verification or []),
                "success_count": int(row.success_count or 0),
                "failure_count": int(row.failure_count or 0),
                "last_outcome_at_ms": row.last_outcome_at_ms,
            }

    async def get_failure(self, memory_id: str) -> dict[str, Any]:
        async with self._session() as db:
            row = await db.get(MemoryFailureProfile, memory_id)
            if row is None:
                raise KeyError("failure profile not found")
            return {
                "memory_id": row.memory_id,
                "signature": row.signature,
                "symptoms": list(row.symptoms or []),
                "root_cause": row.root_cause,
                "attempted_fixes": list(row.attempted_fixes or []),
                "successful_fix": row.successful_fix,
                "verification": row.verification,
                "recurrence_count": int(row.recurrence_count or 0),
                "last_seen_at_ms": int(row.last_seen_at_ms or 0),
            }

    async def metrics(self, memory_ids: list[str]) -> dict[str, dict[str, float]]:
        ids = list(dict.fromkeys(item for item in memory_ids if item))
        if not ids:
            return {}
        async with self._session() as db:
            procedures = list(
                (
                    await db.scalars(
                        select(MemoryProcedureProfile).where(
                            MemoryProcedureProfile.memory_id.in_(ids)
                        )
                    )
                ).all()
            )
            failures = list(
                (
                    await db.scalars(
                        select(MemoryFailureProfile).where(MemoryFailureProfile.memory_id.in_(ids))
                    )
                ).all()
            )
        result: dict[str, dict[str, float]] = {}
        for row in procedures:
            success = int(row.success_count or 0)
            failure = int(row.failure_count or 0)
            total = success + failure
            result[row.memory_id] = {
                "procedure_success": (success + 1.0) / (total + 2.0),
                "failure_recurrence": 0.0,
            }
        for row in failures:
            result.setdefault(row.memory_id, {})["failure_recurrence"] = min(
                1.0, math_log_recurrence(int(row.recurrence_count or 1))
            )
        return result

    async def counts(self, *, user_id: str, workspace: str | None = None) -> dict[str, int]:
        proc_predicates = [MemoryProcedureProfile.user_id == user_id]
        failure_predicates = [MemoryFailureProfile.user_id == user_id]
        if workspace is not None:
            proc_predicates.append(MemoryProcedureProfile.workspace == str(workspace or ""))
            failure_predicates.append(MemoryFailureProfile.workspace == str(workspace or ""))
        async with self._session() as db:
            procedures = list(
                (
                    await db.scalars(
                        select(MemoryProcedureProfile.memory_id).where(*proc_predicates)
                    )
                ).all()
            )
            failures = list(
                (
                    await db.scalars(
                        select(MemoryFailureProfile.memory_id).where(*failure_predicates)
                    )
                ).all()
            )
        return {"procedures": len(procedures), "failures": len(failures)}


def math_log_recurrence(value: int) -> float:
    if value <= 1:
        return 0.25
    # Saturates gradually: recurrence is a useful relevance signal, not proof of correctness.
    import math

    return min(1.0, math.log1p(value) / math.log(16))
