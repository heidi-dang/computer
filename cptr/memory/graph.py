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
        memory_id: str | None = None,
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
                        source_memory_ids=[memory_id] if memory_id else [],
                        status="active",
                        valid_from_ms=valid_from_ms if valid_from_ms is not None else now,
                        created_at_ms=now,
                        updated_at_ms=now,
                    )
                    db.add(row)
                    await db.flush()
                else:
                    changed = False
                    if alias:
                        aliases = list(row.aliases or [])
                        safe_alias = redact_text(alias).strip()[:500]
                        if safe_alias and safe_alias not in aliases:
                            aliases.append(safe_alias)
                            row.aliases = aliases[:100]
                            changed = True
                    if memory_id:
                        sources = list(row.source_memory_ids or [])
                        if memory_id not in sources:
                            sources.append(memory_id)
                            row.source_memory_ids = sources[:200]
                            changed = True
                    if changed:
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
                    memory_id=memory_id,
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

    async def apply_causal_interference(
        self, *, user_id: str, workspace: str, causal_signals: list[dict[str, Any]]
    ) -> dict[str, int]:
        workspace = str(workspace or "")
        now = _now_ms()
        updated_count = 0
        archived_count = 0

        signal_map: dict[str, int] = {}
        for signal in causal_signals:
            mem_id = signal.get("memory_id")
            delta = signal.get("delta_ppm", 0)
            if mem_id and delta:
                signal_map[str(mem_id)] = signal_map.get(str(mem_id), 0) + int(delta)

        if not signal_map:
            return {"updated": 0, "archived": 0}

        async with self._session() as db:
            async with db.begin():
                relationships = list(
                    (
                        await db.scalars(
                            select(MemoryRelationship).where(
                                MemoryRelationship.user_id == user_id,
                                MemoryRelationship.workspace == workspace,
                                MemoryRelationship.status == "active",
                            )
                        )
                    ).all()
                )

                for row in relationships:
                    sources = list(row.source_memory_ids or [])
                    edge_delta = sum(signal_map.get(str(src), 0) for src in sources)

                    if edge_delta != 0:
                        current_ppm = int(row.confidence_ppm or 850_000)
                        new_ppm = max(0, min(1_000_000, current_ppm + edge_delta))

                        if new_ppm < 100_000:
                            row.status = "archived"
                            row.updated_at_ms = now
                            archived_count += 1
                        elif new_ppm != current_ppm:
                            row.confidence_ppm = new_ppm
                            row.updated_at_ms = now
                            updated_count += 1

        return {"updated": updated_count, "archived": archived_count}

    async def clear_scope(self, *, user_id: str, workspace: str) -> None:
        workspace = str(workspace or "")
        async with self._session() as db:
            async with db.begin():
                relationships = list(
                    (
                        await db.scalars(
                            select(MemoryRelationship).where(
                                MemoryRelationship.user_id == user_id,
                                MemoryRelationship.workspace == workspace,
                            )
                        )
                    ).all()
                )
                for row in relationships:
                    await db.delete(row)
                entities = list(
                    (
                        await db.scalars(
                            select(MemoryEntity).where(
                                MemoryEntity.user_id == user_id,
                                MemoryEntity.workspace == workspace,
                            )
                        )
                    ).all()
                )
                for row in entities:
                    await db.delete(row)

    async def related_memory_scores(
        self,
        *,
        user_id: str,
        workspace: str,
        query: str,
        limit: int = 500,
    ) -> dict[str, float]:
        normalized_query = _normalize(query)
        query_terms = set(re.findall(r"[a-z0-9_.:/#-]+", normalized_query))
        if not normalized_query or not query_terms:
            return {}
        workspace = str(workspace or "")
        entity_predicates = [MemoryEntity.user_id == user_id, MemoryEntity.status == "active"]
        relationship_predicates = [
            MemoryRelationship.user_id == user_id,
            MemoryRelationship.status == "active",
        ]
        if workspace:
            entity_predicates.append(
                or_(MemoryEntity.workspace == "", MemoryEntity.workspace == workspace)
            )
            relationship_predicates.append(
                or_(MemoryRelationship.workspace == "", MemoryRelationship.workspace == workspace)
            )
        else:
            entity_predicates.append(MemoryEntity.workspace == "")
            relationship_predicates.append(MemoryRelationship.workspace == "")
        safe_limit = max(1, min(int(limit), 2000))
        async with self._session() as db:
            entities = list(
                (
                    await db.scalars(
                        select(MemoryEntity).where(*entity_predicates).limit(safe_limit)
                    )
                ).all()
            )
            entity_scores: dict[str, float] = {}
            for entity in entities:
                terms = set(re.findall(r"[a-z0-9_.:/#-]+", entity.normalized_name or ""))
                overlap = len(query_terms & terms)
                phrase = (
                    normalized_query in str(entity.normalized_name or "")
                    or str(entity.normalized_name or "") in normalized_query
                )
                score = min(1.0, (0.7 if phrase else 0.0) + 0.3 * overlap / max(1, len(terms)))
                if score >= 0.2:
                    entity_scores[entity.id] = score
            if not entity_scores:
                return {}
            matched_ids = list(entity_scores)
            relationships = list(
                (
                    await db.scalars(
                        select(MemoryRelationship)
                        .where(
                            *relationship_predicates,
                            or_(
                                MemoryRelationship.source_entity_id.in_(matched_ids),
                                MemoryRelationship.target_entity_id.in_(matched_ids),
                            ),
                        )
                        .limit(safe_limit * 2)
                    )
                ).all()
            )

        by_id = {entity.id: entity for entity in entities}
        scores: dict[str, float] = {}
        for entity_id, entity_score in entity_scores.items():
            entity = by_id.get(entity_id)
            if entity is None:
                continue
            for memory_id in list(entity.source_memory_ids or []):
                scores[str(memory_id)] = max(scores.get(str(memory_id), 0.0), entity_score)
        for relationship in relationships:
            endpoint_scores = [
                entity_scores.get(str(relationship.source_entity_id), 0.0),
                entity_scores.get(str(relationship.target_entity_id), 0.0),
            ]
            relation_score = max(endpoint_scores) * 0.82
            for memory_id in list(relationship.source_memory_ids or []):
                scores[str(memory_id)] = max(scores.get(str(memory_id), 0.0), relation_score)
            related_entity_id = (
                relationship.target_entity_id
                if relationship.source_entity_id in entity_scores
                else relationship.source_entity_id
            )
            related = by_id.get(str(related_entity_id))
            if related is not None:
                for memory_id in list(related.source_memory_ids or []):
                    scores[str(memory_id)] = max(
                        scores.get(str(memory_id), 0.0), relation_score * 0.75
                    )
        return scores

    async def remove_memory(
        self, *, user_id: str, workspace: str, memory_id: str
    ) -> dict[str, int]:
        workspace = str(workspace or "")
        removed_relationships = 0
        updated_relationships = 0
        removed_entities = 0
        updated_entities = 0
        async with self._session() as db:
            async with db.begin():
                relationships = list(
                    (
                        await db.scalars(
                            select(MemoryRelationship).where(
                                MemoryRelationship.user_id == user_id,
                                MemoryRelationship.workspace == workspace,
                            )
                        )
                    ).all()
                )
                for row in relationships:
                    sources = list(row.source_memory_ids or [])
                    if memory_id not in sources:
                        continue
                    remaining = [item for item in sources if item != memory_id]
                    if remaining:
                        row.source_memory_ids = remaining
                        row.updated_at_ms = _now_ms()
                        updated_relationships += 1
                    else:
                        await db.delete(row)
                        removed_relationships += 1

                entities = list(
                    (
                        await db.scalars(
                            select(MemoryEntity).where(
                                MemoryEntity.user_id == user_id,
                                MemoryEntity.workspace == workspace,
                            )
                        )
                    ).all()
                )
                for row in entities:
                    sources = list(row.source_memory_ids or [])
                    if memory_id not in sources:
                        continue
                    remaining = [item for item in sources if item != memory_id]
                    if remaining:
                        row.source_memory_ids = remaining
                        row.updated_at_ms = _now_ms()
                        updated_entities += 1
                    else:
                        await db.delete(row)
                        removed_entities += 1
        return {
            "removed_relationships": removed_relationships,
            "updated_relationships": updated_relationships,
            "removed_entities": removed_entities,
            "updated_entities": updated_entities,
        }

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
                    "source_memory_ids": list(row.source_memory_ids or []),
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
