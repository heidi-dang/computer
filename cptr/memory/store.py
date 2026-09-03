"""SQL persistence adapter for the embedded CPTR Memory Core."""

from __future__ import annotations

import hashlib
import time
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import or_, select

from cptr.memory.domain import BranchRef, Checkpoint, MemoryRecordRef, SnapshotRef
from cptr.models import (
    MemoryBranch,
    MemoryCheckpoint,
    MemoryNamespaceState,
    MemoryRecord,
    MemoryRetrievalFeedback,
    MemoryRetrievalProfile,
    MemorySnapshot,
)
from cptr.memory.retrieval import DEFAULT_RETRIEVAL_WEIGHTS, normalize_retrieval_weights
from cptr.utils.db import get_session_factory
from cptr.utils.redaction import redact_sensitive, redact_text


def _now_ms() -> int:
    return int(time.time() * 1000)


def _workspace(value: str | None) -> str:
    return str(value or "")


def _ppm(value: float | int, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, float) and value <= 1.0:
        return max(0, min(1_000_000, int(value * 1_000_000)))
    return max(0, min(1_000_000, int(value)))


def _hash_text(text: str) -> str:
    normalized = " ".join(redact_text(str(text)).strip().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _record_dict(row: MemoryRecord) -> dict[str, Any]:
    return {
        "memory_id": row.id,
        "user_id": row.user_id,
        "workspace": row.workspace,
        "scope": row.scope,
        "kind": row.kind,
        "canonical_text": row.canonical_text,
        "structured_value": row.structured_value if isinstance(row.structured_value, dict) else {},
        "content_hash": row.content_hash,
        "confidence_ppm": int(row.confidence_ppm or 0),
        "importance_ppm": int(row.importance_ppm or 0),
        "trust_level": row.trust_level,
        "status": row.status,
        "valid_from_ms": row.valid_from_ms,
        "valid_until_ms": row.valid_until_ms,
        "observed_at_ms": int(row.observed_at_ms or row.created_at_ms or 0),
        "superseded_at_ms": row.superseded_at_ms,
        "superseded_by_id": row.superseded_by_id,
        "source_event_ids": list(row.source_event_ids or []),
        "branch_id": row.branch_id,
        "parent_memory_id": row.parent_memory_id,
        "verified_at_ms": row.verified_at_ms,
        "verification_expires_at_ms": row.verification_expires_at_ms,
        "access_count": int(row.access_count or 0),
        "last_accessed_at_ms": row.last_accessed_at_ms,
        "created_at_ms": int(row.created_at_ms),
        "updated_at_ms": int(row.updated_at_ms),
    }


class SqlMemoryStore:
    """Authoritative canonical-memory adapter; derived indexes remain rebuildable."""

    def __init__(self, *, session_factory=None) -> None:
        self._session_factory = session_factory or get_session_factory()

    @property
    def session_factory(self):
        return self._session_factory

    @asynccontextmanager
    async def _session(self):
        async with self._session_factory() as db:
            yield db

    async def _namespace_in_session(self, db, user_id: str, workspace: str) -> MemoryNamespaceState:
        workspace = _workspace(workspace)
        row = (
            await db.execute(
                select(MemoryNamespaceState).where(
                    MemoryNamespaceState.user_id == user_id,
                    MemoryNamespaceState.workspace == workspace,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = MemoryNamespaceState(
                user_id=user_id,
                workspace=workspace,
                version=0,
                updated_at_ms=_now_ms(),
            )
            db.add(row)
            await db.flush()
        return row

    async def _bump_version_in_session(self, db, user_id: str, workspace: str) -> int:
        namespace = await self._namespace_in_session(db, user_id, workspace)
        namespace.version = int(namespace.version or 0) + 1
        namespace.updated_at_ms = _now_ms()
        return int(namespace.version)

    async def namespace_version(self, user_id: str, workspace: str) -> int:
        async with self._session() as db:
            row = (
                await db.execute(
                    select(MemoryNamespaceState.version).where(
                        MemoryNamespaceState.user_id == user_id,
                        MemoryNamespaceState.workspace == _workspace(workspace),
                    )
                )
            ).scalar_one_or_none()
        return int(row or 0)

    async def bump_version(self, user_id: str, workspace: str) -> int:
        async with self._session() as db:
            async with db.begin():
                return await self._bump_version_in_session(db, user_id, workspace)

    async def create_memory(
        self,
        *,
        user_id: str,
        workspace: str,
        scope: str,
        kind: str,
        canonical_text: str,
        structured_value: dict[str, Any] | None = None,
        confidence_ppm: int = 850_000,
        importance_ppm: int = 500_000,
        trust_level: str = "agent_observation",
        status: str = "active",
        valid_from_ms: int | None = None,
        valid_until_ms: int | None = None,
        source_event_ids: list[str] | None = None,
        branch_id: str | None = None,
        parent_memory_id: str | None = None,
        verified_at_ms: int | None = None,
        verification_expires_at_ms: int | None = None,
        created_at_ms: int | None = None,
        updated_at_ms: int | None = None,
    ) -> MemoryRecordRef:
        now = int(created_at_ms if created_at_ms is not None else _now_ms())
        updated = int(updated_at_ms if updated_at_ms is not None else now)
        text = redact_text(str(canonical_text).strip())
        if not text:
            raise ValueError("canonical memory text must not be blank")
        workspace = _workspace(workspace)
        async with self._session() as db:
            async with db.begin():
                namespace = await self._namespace_in_session(db, user_id, workspace)
                effective_branch = branch_id or namespace.active_branch_id
                row = MemoryRecord(
                    user_id=user_id,
                    workspace=workspace,
                    scope=scope,
                    kind=kind or "semantic",
                    canonical_text=text,
                    structured_value=redact_sensitive(structured_value or {}),
                    content_hash=_hash_text(text),
                    confidence_ppm=_ppm(confidence_ppm, 850_000),
                    importance_ppm=_ppm(importance_ppm, 500_000),
                    trust_level=trust_level or "agent_observation",
                    status=status or "active",
                    valid_from_ms=valid_from_ms if valid_from_ms is not None else now,
                    valid_until_ms=valid_until_ms,
                    observed_at_ms=now,
                    source_event_ids=list(dict.fromkeys(source_event_ids or []))[:200],
                    branch_id=effective_branch,
                    parent_memory_id=parent_memory_id,
                    verified_at_ms=verified_at_ms,
                    verification_expires_at_ms=verification_expires_at_ms,
                    created_at_ms=now,
                    updated_at_ms=updated,
                )
                db.add(row)
                await db.flush()
                await self._bump_version_in_session(db, user_id, workspace)
                return MemoryRecordRef(row.id, row.kind, row.status, row.branch_id)

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        async with self._session() as db:
            row = await db.get(MemoryRecord, memory_id)
            if row is None:
                raise KeyError("memory not found")
            return _record_dict(row)

    async def find_active_by_hash(
        self,
        user_id: str,
        workspace: str,
        content_hash: str,
        *,
        branch_id: str | None = None,
    ) -> dict[str, Any] | None:
        predicates = [
            MemoryRecord.user_id == user_id,
            MemoryRecord.workspace == _workspace(workspace),
            MemoryRecord.content_hash == content_hash,
            MemoryRecord.status == "active",
        ]
        if branch_id is None:
            predicates.append(MemoryRecord.branch_id.is_(None))
        else:
            predicates.append(MemoryRecord.branch_id == branch_id)
        async with self._session() as db:
            row = (
                await db.execute(
                    select(MemoryRecord)
                    .where(*predicates)
                    .order_by(MemoryRecord.updated_at_ms.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return _record_dict(row) if row is not None else None

    async def merge_source_events(self, memory_id: str, event_ids: list[str]) -> MemoryRecordRef:
        async with self._session() as db:
            async with db.begin():
                row = await db.get(MemoryRecord, memory_id)
                if row is None:
                    raise KeyError("memory not found")
                row.source_event_ids = list(
                    dict.fromkeys([*(row.source_event_ids or []), *event_ids])
                )[:200]
                now = _now_ms()
                row.observed_at_ms = now
                row.updated_at_ms = now
                await self._bump_version_in_session(db, row.user_id, row.workspace)
                return MemoryRecordRef(row.id, row.kind, row.status, row.branch_id)

    async def list_candidates(
        self,
        *,
        user_id: str,
        workspace: str,
        include_historical: bool,
        scope: str = "both",
        kinds: tuple[str, ...] = (),
        branch_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        workspace = _workspace(workspace)
        predicates = [MemoryRecord.user_id == user_id]
        if workspace:
            predicates.append(
                or_(MemoryRecord.workspace == "", MemoryRecord.workspace == workspace)
            )
        else:
            predicates.append(MemoryRecord.workspace == "")
        if not include_historical:
            predicates.append(MemoryRecord.status == "active")
        if scope in {"user", "workspace"}:
            predicates.append(MemoryRecord.scope == scope)
        if kinds:
            predicates.append(MemoryRecord.kind.in_(list(kinds)))
        if branch_id:
            predicates.append(
                or_(MemoryRecord.branch_id.is_(None), MemoryRecord.branch_id == branch_id)
            )
        else:
            predicates.append(MemoryRecord.branch_id.is_(None))
        safe_limit = max(1, min(int(limit), 2000))
        async with self._session() as db:
            rows = list(
                (
                    await db.scalars(
                        select(MemoryRecord)
                        .where(*predicates)
                        .order_by(MemoryRecord.updated_at_ms.desc())
                        .limit(safe_limit)
                    )
                ).all()
            )
        return [_record_dict(row) for row in rows]

    async def list_records_page(
        self,
        user_id: str,
        workspace: str,
        *,
        offset: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        workspace = _workspace(workspace)
        predicates = [MemoryRecord.user_id == user_id]
        if workspace:
            predicates.append(
                or_(MemoryRecord.workspace == "", MemoryRecord.workspace == workspace)
            )
        else:
            predicates.append(MemoryRecord.workspace == "")
        async with self._session() as db:
            rows = list(
                (
                    await db.scalars(
                        select(MemoryRecord)
                        .where(*predicates)
                        .order_by(MemoryRecord.created_at_ms.asc(), MemoryRecord.id.asc())
                        .offset(max(0, int(offset)))
                        .limit(max(1, min(int(limit), 1000)))
                    )
                ).all()
            )
        return [_record_dict(row) for row in rows]

    async def supersede_memory(
        self,
        *,
        old_memory_id: str,
        user_id: str,
        workspace: str,
        scope: str,
        kind: str,
        canonical_text: str,
        structured_value: dict[str, Any],
        source_event_ids: list[str],
        trust_level: str,
        confidence_ppm: int,
        importance_ppm: int,
        valid_from_ms: int,
        verification_expires_at_ms: int | None,
        branch_id: str | None,
    ) -> MemoryRecordRef:
        workspace = _workspace(workspace)
        now = _now_ms()
        text = redact_text(canonical_text.strip())
        async with self._session() as db:
            async with db.begin():
                old = await db.get(MemoryRecord, old_memory_id)
                if old is None or old.user_id != user_id or old.workspace != workspace:
                    raise KeyError("memory not found")
                if old.status != "active":
                    raise ValueError("only active memory can be superseded")
                if branch_id:
                    replacement_branch = branch_id
                else:
                    namespace = await self._namespace_in_session(db, user_id, workspace)
                    replacement_branch = namespace.active_branch_id
                replacement = MemoryRecord(
                    user_id=user_id,
                    workspace=workspace,
                    scope=scope,
                    kind=kind,
                    canonical_text=text,
                    structured_value=redact_sensitive(structured_value),
                    content_hash=_hash_text(text),
                    confidence_ppm=_ppm(confidence_ppm, 900_000),
                    importance_ppm=_ppm(importance_ppm, 500_000),
                    trust_level=trust_level,
                    status="active",
                    valid_from_ms=valid_from_ms,
                    observed_at_ms=now,
                    source_event_ids=list(dict.fromkeys(source_event_ids))[:200],
                    branch_id=replacement_branch,
                    parent_memory_id=old.id,
                    verification_expires_at_ms=verification_expires_at_ms,
                    created_at_ms=now,
                    updated_at_ms=now,
                )
                db.add(replacement)
                await db.flush()
                # Branch replacements shadow the parent without mutating the canonical main timeline.
                if replacement_branch is None:
                    old.status = "superseded"
                    old.valid_until_ms = valid_from_ms
                    old.superseded_at_ms = now
                    old.superseded_by_id = replacement.id
                    old.updated_at_ms = now
                await self._bump_version_in_session(db, user_id, workspace)
                return MemoryRecordRef(
                    replacement.id,
                    replacement.kind,
                    replacement.status,
                    replacement.branch_id,
                )

    async def save_checkpoint(
        self,
        *,
        user_id: str,
        workspace: str,
        task_key: str,
        stage: str,
        state: dict[str, Any],
        memory_version: int | None,
    ) -> Checkpoint:
        workspace = _workspace(workspace)
        async with self._session() as db:
            async with db.begin():
                latest = (
                    await db.execute(
                        select(MemoryCheckpoint)
                        .where(
                            MemoryCheckpoint.user_id == user_id,
                            MemoryCheckpoint.workspace == workspace,
                            MemoryCheckpoint.task_key == task_key,
                        )
                        .order_by(MemoryCheckpoint.version.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                version = int(latest.version if latest else 0) + 1
                current_memory_version = (
                    int(memory_version)
                    if memory_version is not None
                    else int(
                        (await self._namespace_in_session(db, user_id, workspace)).version or 0
                    )
                )
                now = _now_ms()
                row = MemoryCheckpoint(
                    user_id=user_id,
                    workspace=workspace,
                    task_key=task_key,
                    version=version,
                    stage=stage,
                    state=redact_sensitive(state),
                    memory_version=current_memory_version,
                    parent_checkpoint_id=latest.id if latest else None,
                    created_at_ms=now,
                    updated_at_ms=now,
                )
                db.add(row)
                await db.flush()
                return Checkpoint(row.id, version, stage, current_memory_version, now)

    async def latest_checkpoint(
        self, user_id: str, workspace: str, task_key: str
    ) -> dict[str, Any] | None:
        if not task_key:
            return None
        async with self._session() as db:
            row = (
                await db.execute(
                    select(MemoryCheckpoint)
                    .where(
                        MemoryCheckpoint.user_id == user_id,
                        MemoryCheckpoint.workspace == _workspace(workspace),
                        MemoryCheckpoint.task_key == task_key,
                    )
                    .order_by(MemoryCheckpoint.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "checkpoint_id": row.id,
            "version": int(row.version),
            "stage": row.stage,
            "state": row.state if isinstance(row.state, dict) else {},
            "memory_version": int(row.memory_version or 0),
            "created_at_ms": int(row.created_at_ms),
        }

    async def verify_memory(
        self,
        *,
        memory_id: str,
        user_id: str,
        workspace: str,
        verified_at_ms: int,
        verification_expires_at_ms: int | None,
        confidence_ppm: int,
    ) -> dict[str, Any]:
        async with self._session() as db:
            async with db.begin():
                row = await db.get(MemoryRecord, memory_id)
                if row is None or row.user_id != user_id or row.workspace != _workspace(workspace):
                    raise KeyError("memory not found")
                row.verified_at_ms = verified_at_ms
                row.verification_expires_at_ms = verification_expires_at_ms
                row.confidence_ppm = _ppm(confidence_ppm, int(row.confidence_ppm or 0))
                row.updated_at_ms = verified_at_ms
                await self._bump_version_in_session(db, user_id, workspace)
                return _record_dict(row)

    async def record_feedback(
        self,
        *,
        user_id: str,
        workspace: str,
        memory_id: str,
        context_id: str,
        query_hash: str,
        rank: int,
        score_ppm: int,
        used: bool,
        helpful: bool | None,
        outcome: str | None,
        features: dict[str, float] | None = None,
    ) -> None:
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                memory = await db.get(MemoryRecord, memory_id)
                if memory is None or memory.user_id != user_id:
                    raise KeyError("memory not found")
                memory.access_count = int(memory.access_count or 0) + 1
                memory.last_accessed_at_ms = now
                db.add(
                    MemoryRetrievalFeedback(
                        user_id=user_id,
                        workspace=_workspace(workspace),
                        memory_id=memory_id,
                        context_id=context_id,
                        query_hash=query_hash,
                        rank=max(0, int(rank)),
                        score_ppm=_ppm(score_ppm, 0),
                        used=bool(used),
                        helpful=helpful,
                        outcome=redact_text(outcome) if outcome else None,
                        features={
                            str(key): max(0.0, min(1.0, float(value)))
                            for key, value in (features or {}).items()
                            if key in DEFAULT_RETRIEVAL_WEIGHTS
                        },
                        created_at_ms=now,
                    )
                )

    async def list_feedback(
        self, user_id: str, memory_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with self._session() as db:
            rows = list(
                (
                    await db.scalars(
                        select(MemoryRetrievalFeedback)
                        .where(
                            MemoryRetrievalFeedback.user_id == user_id,
                            MemoryRetrievalFeedback.memory_id == memory_id,
                        )
                        .order_by(MemoryRetrievalFeedback.created_at_ms.desc())
                        .limit(max(1, min(int(limit), 200)))
                    )
                ).all()
            )
        return [
            {
                "feedback_id": row.id,
                "context_id": row.context_id,
                "rank": int(row.rank),
                "score": int(row.score_ppm or 0) / 1_000_000,
                "used": bool(row.used),
                "helpful": row.helpful,
                "outcome": row.outcome,
                "features": row.features if isinstance(row.features, dict) else {},
                "created_at_ms": int(row.created_at_ms),
            }
            for row in rows
        ]

    async def get_retrieval_profile(self, user_id: str, workspace: str) -> dict[str, Any]:
        async with self._session() as db:
            row = (
                await db.execute(
                    select(MemoryRetrievalProfile).where(
                        MemoryRetrievalProfile.user_id == user_id,
                        MemoryRetrievalProfile.workspace == _workspace(workspace),
                    )
                )
            ).scalar_one_or_none()
        return {
            "weights": normalize_retrieval_weights(
                row.weights if row is not None and isinstance(row.weights, dict) else None
            ),
            "observations": int(row.observations or 0) if row is not None else 0,
            "updated_at_ms": int(row.updated_at_ms or 0) if row is not None else 0,
        }

    async def learn_retrieval_profile(
        self,
        user_id: str,
        workspace: str,
        *,
        features: dict[str, float],
        positive: bool,
    ) -> dict[str, Any]:
        safe_features = {
            key: max(0.0, min(1.0, float(value)))
            for key, value in features.items()
            if key in DEFAULT_RETRIEVAL_WEIGHTS
        }
        workspace = _workspace(workspace)
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(MemoryRetrievalProfile).where(
                            MemoryRetrievalProfile.user_id == user_id,
                            MemoryRetrievalProfile.workspace == workspace,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = MemoryRetrievalProfile(
                        user_id=user_id,
                        workspace=workspace,
                        weights=dict(DEFAULT_RETRIEVAL_WEIGHTS),
                        observations=0,
                        updated_at_ms=now,
                    )
                    db.add(row)
                    await db.flush()
                current = normalize_retrieval_weights(
                    row.weights if isinstance(row.weights, dict) else None
                )
                observations = int(row.observations or 0)
                learning_rate = max(0.025, 0.18 / (1.0 + observations / 20.0))
                for key, feature in safe_features.items():
                    target = feature if positive else 1.0 - feature
                    current[key] = current[key] * (1.0 - learning_rate) + target * learning_rate
                row.weights = normalize_retrieval_weights(current)
                row.observations = observations + 1
                row.updated_at_ms = now
                await db.flush()
                return {
                    "weights": dict(row.weights),
                    "observations": int(row.observations),
                    "updated_at_ms": now,
                }

    async def create_snapshot(
        self, user_id: str, workspace: str, *, label: str = ""
    ) -> SnapshotRef:
        workspace = _workspace(workspace)
        async with self._session() as db:
            async with db.begin():
                namespace = await self._namespace_in_session(db, user_id, workspace)
                rows = list(
                    (
                        await db.scalars(
                            select(MemoryRecord)
                            .where(
                                MemoryRecord.user_id == user_id,
                                MemoryRecord.workspace.in_(["", workspace])
                                if workspace
                                else MemoryRecord.workspace == "",
                                MemoryRecord.status == "active",
                                MemoryRecord.branch_id.is_(None),
                            )
                            .order_by(MemoryRecord.id)
                        )
                    ).all()
                )
                now = _now_ms()
                row = MemorySnapshot(
                    user_id=user_id,
                    workspace=workspace,
                    label=redact_text(label)[:500],
                    memory_version=int(namespace.version or 0),
                    manifest={
                        "memory_ids": [item.id for item in rows],
                        "record_count": len(rows),
                    },
                    created_at_ms=now,
                )
                db.add(row)
                await db.flush()
                return SnapshotRef(row.id, int(row.memory_version), row.label, now)

    async def get_snapshot(self, user_id: str, workspace: str, snapshot_id: str) -> dict[str, Any]:
        async with self._session() as db:
            row = await db.get(MemorySnapshot, snapshot_id)
            if row is None or row.user_id != user_id or row.workspace != _workspace(workspace):
                raise KeyError("memory snapshot not found")
            return {
                "snapshot_id": row.id,
                "label": row.label,
                "memory_version": int(row.memory_version),
                "manifest": row.manifest if isinstance(row.manifest, dict) else {},
                "created_at_ms": int(row.created_at_ms),
            }

    async def create_branch(
        self,
        user_id: str,
        workspace: str,
        *,
        name: str,
        from_snapshot_id: str | None,
        activate: bool = False,
    ) -> BranchRef:
        workspace = _workspace(workspace)
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                if from_snapshot_id:
                    snapshot = await db.get(MemorySnapshot, from_snapshot_id)
                    if (
                        snapshot is None
                        or snapshot.user_id != user_id
                        or snapshot.workspace != workspace
                    ):
                        raise KeyError("memory snapshot not found")
                row = MemoryBranch(
                    user_id=user_id,
                    workspace=workspace,
                    name=redact_text(name).strip()[:240] or "branch",
                    from_snapshot_id=from_snapshot_id,
                    status="active",
                    created_at_ms=now,
                    updated_at_ms=now,
                )
                db.add(row)
                await db.flush()
                if activate:
                    namespace = await self._namespace_in_session(db, user_id, workspace)
                    namespace.active_branch_id = row.id
                    namespace.active_snapshot_id = from_snapshot_id
                    namespace.updated_at_ms = now
                    namespace.version = int(namespace.version or 0) + 1
                return BranchRef(row.id, row.name, row.from_snapshot_id, now)

    async def restore_snapshot(self, user_id: str, workspace: str, snapshot_id: str) -> SnapshotRef:
        workspace = _workspace(workspace)
        snapshot = await self.get_snapshot(user_id, workspace, snapshot_id)
        # Restore is non-destructive: activate a branch anchored to the immutable snapshot.
        await self.create_branch(
            user_id,
            workspace,
            name=f"restore-{snapshot_id[-8:]}",
            from_snapshot_id=snapshot_id,
            activate=True,
        )
        return SnapshotRef(
            snapshot["snapshot_id"],
            snapshot["memory_version"],
            snapshot["label"],
            snapshot["created_at_ms"],
        )

    async def supersede_with_existing(
        self,
        *,
        old_memory_id: str,
        new_memory_id: str,
        user_id: str,
        workspace: str,
    ) -> None:
        workspace = _workspace(workspace)
        async with self._session() as db:
            async with db.begin():
                old = await db.get(MemoryRecord, old_memory_id)
                new = await db.get(MemoryRecord, new_memory_id)
                if (
                    old is None
                    or new is None
                    or old.user_id != user_id
                    or new.user_id != user_id
                    or old.workspace != workspace
                    or new.workspace != workspace
                ):
                    raise KeyError("memory not found")
                if old.id == new.id:
                    raise ValueError("memory cannot supersede itself")
                valid_from = int(new.valid_from_ms or new.created_at_ms or _now_ms())
                old.status = "superseded"
                old.valid_until_ms = valid_from
                old.superseded_by_id = new.id
                old.updated_at_ms = _now_ms()
                await self._bump_version_in_session(db, user_id, workspace)

    async def begin_forget(self, memory_id: str, *, user_id: str, workspace: str) -> dict[str, Any]:
        workspace = _workspace(workspace)
        async with self._session() as db:
            async with db.begin():
                row = await db.get(MemoryRecord, memory_id)
                if row is None or row.user_id != user_id or row.workspace != workspace:
                    raise KeyError("memory not found")
                row.status = "deleting"
                row.updated_at_ms = _now_ms()
                await self._bump_version_in_session(db, user_id, workspace)
                await db.flush()
                return _record_dict(row)

    async def forget_memory(self, memory_id: str, *, user_id: str, workspace: str) -> None:
        workspace = _workspace(workspace)
        async with self._session() as db:
            async with db.begin():
                row = await db.get(MemoryRecord, memory_id)
                if row is None or row.user_id != user_id or row.workspace != workspace:
                    raise KeyError("memory not found")
                await db.delete(row)
                await self._bump_version_in_session(db, user_id, workspace)

    async def mark_disputed(self, memory_id: str, *, user_id: str, workspace: str) -> None:
        workspace = _workspace(workspace)
        async with self._session() as db:
            async with db.begin():
                row = await db.get(MemoryRecord, memory_id)
                if row is None or row.user_id != user_id or row.workspace != workspace:
                    raise KeyError("memory not found")
                row.status = "disputed"
                row.updated_at_ms = _now_ms()
                await self._bump_version_in_session(db, user_id, workspace)

    async def time_travel_records(
        self,
        user_id: str,
        workspace: str,
        *,
        at_ms: int,
        known_at_ms: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        workspace = _workspace(workspace)
        valid_at = int(at_ms)
        known_at = int(known_at_ms) if known_at_ms is not None else None
        predicates = [
            MemoryRecord.user_id == user_id,
            MemoryRecord.branch_id.is_(None),
            MemoryRecord.valid_from_ms <= valid_at,
        ]
        if known_at is None:
            predicates.append(
                or_(MemoryRecord.valid_until_ms.is_(None), MemoryRecord.valid_until_ms > valid_at)
            )
        else:
            predicates.extend(
                [
                    MemoryRecord.observed_at_ms <= known_at,
                    or_(
                        MemoryRecord.superseded_at_ms > known_at,
                        MemoryRecord.valid_until_ms.is_(None),
                        MemoryRecord.valid_until_ms > valid_at,
                    ),
                ]
            )
        if workspace:
            predicates.append(
                or_(MemoryRecord.workspace == "", MemoryRecord.workspace == workspace)
            )
        else:
            predicates.append(MemoryRecord.workspace == "")
        async with self._session() as db:
            rows = list(
                (
                    await db.scalars(
                        select(MemoryRecord)
                        .where(*predicates)
                        .order_by(MemoryRecord.valid_from_ms.asc(), MemoryRecord.id.asc())
                        .limit(max(1, min(int(limit), 5000)))
                    )
                ).all()
            )
        records = [_record_dict(row) for row in rows]
        if known_at is not None:
            for record in records:
                superseded_at = record.get("superseded_at_ms")
                if superseded_at is not None and int(superseded_at) > known_at:
                    if record.get("status") == "superseded":
                        record["status"] = "active"
                    record["valid_until_ms"] = None
                    record["superseded_by_id"] = None
                    record["superseded_at_ms"] = None
        return records

    async def compare_snapshots(
        self,
        user_id: str,
        workspace: str,
        left_snapshot_id: str,
        right_snapshot_id: str,
    ) -> dict[str, Any]:
        left = await self.get_snapshot(user_id, workspace, left_snapshot_id)
        right = await self.get_snapshot(user_id, workspace, right_snapshot_id)
        left_ids = set(str(item) for item in left["manifest"].get("memory_ids", []))
        right_ids = set(str(item) for item in right["manifest"].get("memory_ids", []))
        added = sorted(right_ids - left_ids)
        removed = sorted(left_ids - right_ids)
        changed: list[dict[str, str]] = []
        if added and removed:
            async with self._session() as db:
                rows = list(
                    (
                        await db.scalars(
                            select(MemoryRecord).where(MemoryRecord.id.in_(list(added)))
                        )
                    ).all()
                )
            for row in rows:
                if row.parent_memory_id and row.parent_memory_id in removed:
                    changed.append({"from": row.parent_memory_id, "to": row.id})
        return {
            "left_snapshot_id": left_snapshot_id,
            "right_snapshot_id": right_snapshot_id,
            "left_memory_version": left["memory_version"],
            "right_memory_version": right["memory_version"],
            "added": added,
            "removed": removed,
            "changed": changed,
        }

    async def merge_branch(
        self,
        user_id: str,
        workspace: str,
        branch_id: str,
        *,
        strategy: str = "verified_only",
    ) -> dict[str, Any]:
        if strategy not in {"verified_only", "all"}:
            raise ValueError("unsupported branch merge strategy")
        workspace = _workspace(workspace)
        high_trust = {"user_directive", "verified_system_fact", "tool_result"}
        merged_ids: list[str] = []
        skipped_ids: list[str] = []
        conflicted_ids: list[str] = []
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                branch = await db.get(MemoryBranch, branch_id)
                if branch is None or branch.user_id != user_id or branch.workspace != workspace:
                    raise KeyError("memory branch not found")
                rows = list(
                    (
                        await db.scalars(
                            select(MemoryRecord)
                            .where(
                                MemoryRecord.user_id == user_id,
                                MemoryRecord.workspace == workspace,
                                MemoryRecord.branch_id == branch_id,
                                MemoryRecord.status == "active",
                            )
                            .order_by(MemoryRecord.created_at_ms.asc())
                        )
                    ).all()
                )
                for row in rows:
                    verified = row.verified_at_ms is not None or row.trust_level in high_trust
                    if strategy == "verified_only" and not verified:
                        skipped_ids.append(row.id)
                        continue
                    parent_id = row.parent_memory_id
                    parent = await db.get(MemoryRecord, parent_id) if parent_id else None
                    if parent_id and (
                        parent is None
                        or parent.user_id != user_id
                        or parent.workspace != workspace
                        or parent.branch_id is not None
                        or parent.status != "active"
                        or parent.superseded_by_id is not None
                    ):
                        conflicted_ids.append(row.id)
                        continue
                    existing = (
                        await db.execute(
                            select(MemoryRecord)
                            .where(
                                MemoryRecord.user_id == user_id,
                                MemoryRecord.workspace == workspace,
                                MemoryRecord.branch_id.is_(None),
                                MemoryRecord.status == "active",
                                MemoryRecord.content_hash == row.content_hash,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if existing is None:
                        existing = MemoryRecord(
                            user_id=user_id,
                            workspace=workspace,
                            scope=row.scope,
                            kind=row.kind,
                            canonical_text=row.canonical_text,
                            structured_value=redact_sensitive(row.structured_value or {}),
                            content_hash=row.content_hash,
                            confidence_ppm=int(row.confidence_ppm or 0),
                            importance_ppm=int(row.importance_ppm or 0),
                            trust_level=row.trust_level,
                            status="active",
                            valid_from_ms=now,
                            valid_until_ms=None,
                            observed_at_ms=now,
                            source_event_ids=list(row.source_event_ids or []),
                            branch_id=None,
                            parent_memory_id=row.parent_memory_id or row.id,
                            verified_at_ms=row.verified_at_ms,
                            verification_expires_at_ms=row.verification_expires_at_ms,
                            access_count=0,
                            created_at_ms=now,
                            updated_at_ms=now,
                        )
                        db.add(existing)
                        await db.flush()
                    if parent is not None and parent.id != existing.id:
                        parent.status = "superseded"
                        parent.valid_until_ms = int(existing.valid_from_ms or now)
                        parent.superseded_at_ms = now
                        parent.superseded_by_id = existing.id
                        parent.updated_at_ms = now
                    merged_ids.append(existing.id)
                branch.status = "merged"
                branch.updated_at_ms = now
                namespace = await self._namespace_in_session(db, user_id, workspace)
                if namespace.active_branch_id == branch_id:
                    namespace.active_branch_id = None
                    namespace.active_snapshot_id = None
                namespace.version = int(namespace.version or 0) + 1
                namespace.updated_at_ms = now
        return {
            "branch_id": branch_id,
            "strategy": strategy,
            "merged_count": len(merged_ids),
            "skipped_count": len(skipped_ids),
            "conflicted_count": len(conflicted_ids),
            "merged_memory_ids": merged_ids,
            "skipped_memory_ids": skipped_ids,
            "conflicted_memory_ids": conflicted_ids,
        }

    async def observability_snapshot(
        self,
        *,
        user_id: str,
        workspace: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Return bounded owner-scoped canonical state for the Memory Observatory."""
        safe_limit = max(25, min(int(limit), 2_000))
        workspace_value = None if workspace is None else _workspace(workspace)
        memory_predicates = [MemoryRecord.user_id == user_id]
        if workspace_value is not None:
            if workspace_value:
                memory_predicates.append(
                    or_(MemoryRecord.workspace == "", MemoryRecord.workspace == workspace_value)
                )
            else:
                memory_predicates.append(MemoryRecord.workspace == "")

        async with self._session() as db:
            records = list(
                (
                    await db.scalars(
                        select(MemoryRecord)
                        .where(*memory_predicates)
                        .order_by(MemoryRecord.updated_at_ms.desc(), MemoryRecord.id.desc())
                        .limit(safe_limit)
                    )
                ).all()
            )

            snapshot_query = select(MemorySnapshot).where(MemorySnapshot.user_id == user_id)
            branch_query = select(MemoryBranch).where(MemoryBranch.user_id == user_id)
            checkpoint_query = select(MemoryCheckpoint).where(MemoryCheckpoint.user_id == user_id)
            namespace_query = select(MemoryNamespaceState).where(
                MemoryNamespaceState.user_id == user_id
            )
            if workspace_value is not None:
                snapshot_query = snapshot_query.where(MemorySnapshot.workspace == workspace_value)
                branch_query = branch_query.where(MemoryBranch.workspace == workspace_value)
                checkpoint_query = checkpoint_query.where(
                    MemoryCheckpoint.workspace == workspace_value
                )
                namespace_query = namespace_query.where(
                    MemoryNamespaceState.workspace == workspace_value
                )

            snapshots = list(
                (
                    await db.scalars(
                        snapshot_query.order_by(MemorySnapshot.created_at_ms.desc()).limit(50)
                    )
                ).all()
            )
            branches = list(
                (
                    await db.scalars(
                        branch_query.order_by(MemoryBranch.updated_at_ms.desc()).limit(50)
                    )
                ).all()
            )
            checkpoints = list(
                (
                    await db.scalars(
                        checkpoint_query.order_by(MemoryCheckpoint.created_at_ms.desc()).limit(100)
                    )
                ).all()
            )
            namespaces = list((await db.scalars(namespace_query)).all())

        return {
            "records": [_record_dict(row) for row in records],
            "snapshots": [
                {
                    "snapshot_id": row.id,
                    "workspace": row.workspace,
                    "label": row.label,
                    "memory_version": int(row.memory_version or 0),
                    "record_count": int((row.manifest or {}).get("record_count") or 0)
                    if isinstance(row.manifest, dict)
                    else 0,
                    "created_at_ms": int(row.created_at_ms),
                }
                for row in snapshots
            ],
            "branches": [
                {
                    "branch_id": row.id,
                    "workspace": row.workspace,
                    "name": row.name,
                    "from_snapshot_id": row.from_snapshot_id,
                    "status": row.status,
                    "created_at_ms": int(row.created_at_ms),
                    "updated_at_ms": int(row.updated_at_ms),
                }
                for row in branches
            ],
            "checkpoints": [
                {
                    "checkpoint_id": row.id,
                    "workspace": row.workspace,
                    "task_key_hash": hashlib.sha256(row.task_key.encode("utf-8")).hexdigest()[:16],
                    "version": int(row.version or 0),
                    "stage": row.stage,
                    "memory_version": int(row.memory_version or 0),
                    "created_at_ms": int(row.created_at_ms),
                }
                for row in checkpoints
            ],
            "namespaces": [
                {
                    "workspace": row.workspace,
                    "version": int(row.version or 0),
                    "active_branch_id": row.active_branch_id,
                    "active_snapshot_id": row.active_snapshot_id,
                    "updated_at_ms": int(row.updated_at_ms),
                }
                for row in namespaces
            ],
        }

    async def active_restore_scope(self, user_id: str, workspace: str) -> dict[str, Any]:
        async with self._session() as db:
            namespace = (
                await db.execute(
                    select(MemoryNamespaceState).where(
                        MemoryNamespaceState.user_id == user_id,
                        MemoryNamespaceState.workspace == _workspace(workspace),
                    )
                )
            ).scalar_one_or_none()
            if namespace is None:
                return {
                    "active_branch_id": None,
                    "active_snapshot_id": None,
                    "snapshot_memory_ids": [],
                }
            memory_ids: list[str] = []
            if namespace.active_snapshot_id:
                snapshot = await db.get(MemorySnapshot, namespace.active_snapshot_id)
                if snapshot is not None and isinstance(snapshot.manifest, dict):
                    memory_ids = [str(item) for item in snapshot.manifest.get("memory_ids", [])]
            return {
                "active_branch_id": namespace.active_branch_id,
                "active_snapshot_id": namespace.active_snapshot_id,
                "snapshot_memory_ids": memory_ids,
            }


content_hash = _hash_text
