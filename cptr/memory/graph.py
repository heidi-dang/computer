"""Derived entity/relationship projection for canonical CPTR memory."""

from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import or_, select

from cptr.models import MemoryEntity, MemoryRelationship
from cptr.utils.db import get_session_factory
from cptr.utils.redaction import redact_text

_WIKI_RE = re.compile(r"\[\[([^\]\n|]+)(?:\|[^\]\n]+)?\]\]")
_CODE_ENTITY_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+\.(?:service|timer|socket)|"
    r"PR\s*#\d+|#[0-9]+|[a-f0-9]{7,40})(?![\w])",
    re.IGNORECASE,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize(value: str) -> str:
    return " ".join(redact_text(value).strip().lower().split())[:500]


def _entity_type(value: str) -> str:
    lowered = value.lower()
    if lowered.endswith((".service", ".timer", ".socket")):
        return "service"
    if re.fullmatch(r"[a-f0-9]{7,40}", lowered):
        return "commit"
    if re.fullmatch(r"(?:pr\s*)?#\d+", lowered):
        return "pull_request"
    if "/" in value and " " not in value:
        return "repository_or_path"
    return "concept"


def extract_entities(*, heading: str, text: str) -> list[tuple[str, str]]:
    values: list[str] = []
    heading = redact_text(heading).strip()
    if heading and heading.lower() not in {"baseline", "memory", "notes"}:
        values.append(heading)
    values.extend(match.group(1).strip() for match in _WIKI_RE.finditer(text or ""))
    values.extend(match.group(0).strip() for match in _CODE_ENTITY_RE.finditer(text or ""))
    dedup: dict[tuple[str, str], str] = {}
    for value in values:
        safe = redact_text(value).strip()[:500]
        if not safe:
            continue
        entity_type = _entity_type(safe)
        key = (_normalize(safe), entity_type)
        if key[0]:
            dedup[key] = safe
    return [(display, entity_type) for (_normalized, entity_type), display in dedup.items()]


class MemoryGraphStore:
    """Rebuildable graph adapter. Canonical memory remains the source of truth."""

    def __init__(self, *, session_factory=None) -> None:
        self._session_factory = session_factory or get_session_factory()

    @asynccontextmanager
    async def _session(self):
        async with self._session_factory() as db:
            yield db

    async def upsert_entity(
        self,
        *,
        user_id: str,
        workspace: str,
        name: str,
        entity_type: str | None = None,
        alias: str | None = None,
        valid_from_ms: int | None = None,
    ) -> str:
        safe_name = redact_text(name).strip()[:500]
        normalized = _normalize(safe_name)
        if not normalized:
            raise ValueError("entity name must not be blank")
        kind = entity_type or _entity_type(safe_name)
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(MemoryEntity).where(
                            MemoryEntity.user_id == user_id,
                            MemoryEntity.workspace == str(workspace or ""),
                            MemoryEntity.normalized_name == normalized,
                            MemoryEntity.entity_type == kind,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = MemoryEntity(
                        user_id=user_id,
                        workspace=str(workspace or ""),
                        canonical_name=safe_name,
                        normalized_name=normalized,
                        entity_type=kind,
                        aliases=[redact_text(alias).strip()[:500]] if alias else [],
                        status="active",
                        valid_from_ms=valid_from_ms if valid_from_ms is not None else now,
                        created_at_ms=now,
                        updated_at_ms=now,
                    )
                    db.add(row)
                    await db.flush()
                elif alias:
                    aliases = list(row.aliases or [])
                    safe_alias = redact_text(alias).strip()[:500]
                    if safe_alias and safe_alias not in aliases:
                        aliases.append(safe_alias)
                        row.aliases = aliases[:100]
                        row.updated_at_ms = now
                return row.id

    async def link(
        self,
        *,
        user_id: str,
        workspace: str,
        source_entity_id: str,
        target_entity_id: str,
        relation: str,
        memory_id: str,
        confidence_ppm: int = 850_000,
        valid_from_ms: int | None = None,
    ) -> str:
        relation = _normalize(relation)[:120] or "related_to"
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(MemoryRelationship).where(
                            MemoryRelationship.user_id == user_id,
                            MemoryRelationship.workspace == str(workspace or ""),
                            MemoryRelationship.source_entity_id == source_entity_id,
                            MemoryRelationship.target_entity_id == target_entity_id,
                            MemoryRelationship.relation == relation,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = MemoryRelationship(
                        user_id=user_id,
                        workspace=str(workspace or ""),
                        source_entity_id=source_entity_id,
                        target_entity_id=target_entity_id,
                        relation=relation,
                        confidence_ppm=max(0, min(1_000_000, int(confidence_ppm))),
                        source_memory_ids=[memory_id],
                        status="active",
                        valid_from_ms=valid_from_ms if valid_from_ms is not None else now,
                        created_at_ms=now,
                        updated_at_ms=now,
                    )
                    db.add(row)
                    await db.flush()
                else:
                    sources = list(row.source_memory_ids or [])
                    if memory_id not in sources:
                        sources.append(memory_id)
                        row.source_memory_ids = sources[:200]
                    row.confidence_ppm = max(
                        int(row.confidence_ppm or 0), max(0, min(1_000_000, int(confidence_ppm)))
                    )
                    row.updated_at_ms = now
                return row.id

    async def project_memory(
        self,
        *,
        user_id: str,
        workspace: str,
        memory_id: str,
        heading: str,
        text: str,
        valid_from_ms: int | None = None,
    ) -> dict[str, Any]:
        extracted = extract_entities(heading=heading, text=text)
        if not extracted:
            return {"entity_ids": [], "relationship_ids": []}
        entity_ids: list[str] = []
        for name, entity_type in extracted[:50]:
            entity_ids.append(
                await self.upsert_entity(
                    user_id=user_id,
                    workspace=workspace,
                    name=name,
                    entity_type=entity_type,
                    valid_from_ms=valid_from_ms,
                )
            )
        relationship_ids: list[str] = []
        if len(entity_ids) > 1:
            root = entity_ids[0]
            for target in entity_ids[1:50]:
                if target == root:
                    continue
                relationship_ids.append(
                    await self.link(
                        user_id=user_id,
                        workspace=workspace,
                        source_entity_id=root,
                        target_entity_id=target,
                        relation="related_to",
                        memory_id=memory_id,
                        valid_from_ms=valid_from_ms,
                    )
                )
        return {"entity_ids": entity_ids, "relationship_ids": relationship_ids}

    async def snapshot(
        self, *, user_id: str, workspace: str | None = None, limit: int = 500
    ) -> dict[str, list[dict[str, Any]]]:
        predicates = [MemoryEntity.user_id == user_id, MemoryEntity.status == "active"]
        rel_predicates = [
            MemoryRelationship.user_id == user_id,
            MemoryRelationship.status == "active",
        ]
        if workspace is not None:
            workspace_value = str(workspace or "")
            if workspace_value:
                predicates.append(
                    or_(MemoryEntity.workspace == "", MemoryEntity.workspace == workspace_value)
                )
                rel_predicates.append(
                    or_(
                        MemoryRelationship.workspace == "",
                        MemoryRelationship.workspace == workspace_value,
                    )
                )
            else:
                predicates.append(MemoryEntity.workspace == "")
                rel_predicates.append(MemoryRelationship.workspace == "")
        safe_limit = max(1, min(int(limit), 2000))
        async with self._session() as db:
            entities = list(
                (
                    await db.scalars(
                        select(MemoryEntity)
                        .where(*predicates)
                        .order_by(MemoryEntity.updated_at_ms.desc())
                        .limit(safe_limit)
                    )
                ).all()
            )
            relationships = list(
                (
                    await db.scalars(
                        select(MemoryRelationship)
                        .where(*rel_predicates)
                        .order_by(MemoryRelationship.updated_at_ms.desc())
                        .limit(safe_limit * 2)
                    )
                ).all()
            )
        return {
            "entities": [
                {
                    "entity_id": row.id,
                    "workspace": row.workspace,
                    "canonical_name": row.canonical_name,
                    "entity_type": row.entity_type,
                    "aliases": list(row.aliases or []),
                    "status": row.status,
                    "valid_from_ms": row.valid_from_ms,
                    "valid_until_ms": row.valid_until_ms,
                    "updated_at_ms": int(row.updated_at_ms),
                }
                for row in entities
            ],
            "relationships": [
                {
                    "relationship_id": row.id,
                    "workspace": row.workspace,
                    "source_entity_id": row.source_entity_id,
                    "target_entity_id": row.target_entity_id,
                    "relation": row.relation,
                    "confidence": int(row.confidence_ppm or 0) / 1_000_000,
                    "source_memory_ids": list(row.source_memory_ids or []),
                    "status": row.status,
                    "valid_from_ms": row.valid_from_ms,
                    "valid_until_ms": row.valid_until_ms,
                    "updated_at_ms": int(row.updated_at_ms),
                }
                for row in relationships
            ],
        }


memory_graph_store = MemoryGraphStore()
