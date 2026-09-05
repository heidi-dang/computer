"""Embedded CPTR Memory Core service facade.

CPTR orchestration depends on this service boundary. Storage, retrieval, and future
PostgreSQL/pgvector adapters remain replaceable behind the interface.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from cptr.memory.conflicts import MemoryConflictStore, conflict_classification, fact_signature
from cptr.memory.domain import (
    BranchRef,
    Checkpoint,
    CheckpointState,
    ConsolidationInput,
    ManagedContext,
    MemoryConflictRef,
    MemoryContextBundle,
    MemoryQuery,
    MemoryRecordRef,
    MemoryReplacement,
    MemoryResult,
    PrepareContextInput,
    RetrievalFeedback,
    SnapshotRef,
    VerificationResult,
)
from cptr.memory.embeddings import SqlVectorIndex, embedding_provider_from_env
from cptr.memory.graph import MemoryGraphStore
from cptr.memory.intelligence import MemoryIntelligenceStore
from cptr.memory.jobs import MemoryJobStore
from cptr.memory.lexical import MemoryLexicalIndex
from cptr.memory.pgvector import PgVectorIndex
from cptr.memory.retrieval import VectorSearchPort, score_candidate
from cptr.memory.store import SqlMemoryStore, content_hash
from cptr.services.memory_fabric import MemoryFabricStore, memory_fabric_store
from cptr.utils.redaction import redact_sensitive, redact_text


class MemoryUnavailableError(RuntimeError):
    """Raised when enabled required memory cannot establish pre-reasoning context."""


ManagedLoader = Callable[[PrepareContextInput], Awaitable[ManagedContext]]
SettingsLoader = Callable[[], Awaitable[dict[str, Any]]]
SourceForgetter = Callable[[dict[str, Any]], Awaitable[None]]

_MUTATION_EVENTS = {
    "write",
    "memory_created",
    "memory_superseded",
    "memory_verified",
    "snapshot_restored",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clamp(value: float, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _query_text(value: PrepareContextInput) -> str:
    parts = [value.current_message.strip()]
    parts.extend(str(item).strip() for item in value.mentioned_files if str(item).strip())
    if not parts[0] and value.recent_messages:
        for message in value.recent_messages[-6:]:
            if message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    parts.append(content.strip())
    return "\n".join(part for part in parts if part)[:12_000]


def _infer_kind(text: str, heading: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    value = f"{heading}\n{text}".lower()
    if any(
        token in value for token in ("root cause", "failure", "incident", "regression", "failed")
    ):
        return "failure"
    if any(token in value for token in ("procedure", "runbook", "workflow", "deploy", "steps:")):
        return "procedure"
    if any(token in value for token in ("decision", "decided", "architecture", "adr")):
        return "decision"
    if any(token in value for token in ("preference", "prefer", "always", "never")):
        return "policy"
    return "semantic"


async def _default_settings_loader() -> dict[str, Any]:
    from cptr.utils.memory import get_memory_settings

    return await get_memory_settings()


async def _default_managed_loader(value: PrepareContextInput) -> ManagedContext:
    from cptr.utils.memory import build_memory_prompt_bundle

    rendered, items = await build_memory_prompt_bundle(
        value.runtime_request,
        value.user_id,
        value.workspace,
        current_message=value.current_message,
        recent_messages=value.recent_messages,
        mentioned_files=value.mentioned_files,
    )
    return ManagedContext(rendered=rendered, items=items)


class EmbeddedMemoryService:
    """Single-process Memory Core with strict ports and durable persistence."""

    def __init__(
        self,
        *,
        store: SqlMemoryStore | None = None,
        event_store: MemoryFabricStore | None = None,
        managed_context_loader: ManagedLoader | None = None,
        settings_loader: SettingsLoader | None = None,
        vector_search: VectorSearchPort | None = None,
        lexical_search: MemoryLexicalIndex | None = None,
        conflict_store: MemoryConflictStore | None = None,
        intelligence_store: MemoryIntelligenceStore | None = None,
        job_store: MemoryJobStore | None = None,
        graph_store: MemoryGraphStore | None = None,
    ) -> None:
        self.store = store or SqlMemoryStore()
        self.event_store = event_store or memory_fabric_store
        self._managed_context_loader = managed_context_loader or _default_managed_loader
        self._settings_loader = settings_loader or _default_settings_loader
        session_factory = self.store.session_factory
        if vector_search is None:
            provider = embedding_provider_from_env()
            vector_search = PgVectorIndex.from_env(provider=provider) or SqlVectorIndex(
                session_factory=session_factory,
                provider=provider,
            )
        self._vector_search = vector_search
        self._lexical_search = lexical_search or MemoryLexicalIndex(session_factory=session_factory)
        self.conflict_store = conflict_store or MemoryConflictStore(session_factory=session_factory)
        self.intelligence_store = intelligence_store or MemoryIntelligenceStore(
            session_factory=session_factory
        )
        self.job_store = job_store or MemoryJobStore(session_factory=session_factory)
        self.graph_store = graph_store or MemoryGraphStore(session_factory=session_factory)
        self._index_errors: dict[str, dict[str, Any]] = {}

    async def prepare_context(self, value: PrepareContextInput) -> MemoryContextBundle:
        settings = await self._settings_loader()
        if not bool(settings.get("enabled", True)):
            return MemoryContextBundle(
                context_id=f"memctx_{uuid.uuid4().hex}",
                status="disabled",
                memory_version=await self.store.namespace_version(value.user_id, value.workspace),
                rendered="",
            )

        required = bool(settings.get("required_for_execution", True))
        context_id = f"memctx_{uuid.uuid4().hex}"
        try:
            managed = await self._managed_context_loader(value)
            query = _query_text(value)
            canonical_results = (
                await self.search(
                    MemoryQuery(
                        user_id=value.user_id,
                        workspace=value.workspace,
                        query=query,
                        limit=20,
                    )
                )
                if query
                else []
            )
            canonical_limit = max(500, int(settings.get("canonical_char_limit") or 3000))
            total_limit = max(
                canonical_limit,
                int(value.max_chars or settings.get("context_char_limit") or 9000),
            )
            canonical_lines: list[str] = []
            canonical_chars = 0
            canonical_items: list[dict[str, Any]] = []
            for result in canonical_results:
                if result.score <= 0.025:
                    continue
                prefix = (
                    f"- [{result.kind}; {result.scope}; trust={result.trust_level}; "
                    f"confidence={result.confidence:.2f}] "
                )
                line = prefix + result.canonical_text
                if canonical_chars + len(line) + 1 > canonical_limit:
                    break
                canonical_lines.append(line)
                canonical_chars += len(line) + 1
                canonical_items.append(
                    {
                        "node_id": result.memory_id,
                        "scope": result.scope,
                        "path": "canonical",
                        "heading": result.kind,
                        "memory_id": result.memory_id,
                        "reason": result.reason,
                        "score": result.score,
                        "verification_stale": result.verification_stale,
                    }
                )

            canonical_block = ""
            if canonical_lines:
                canonical_block = "[Canonical Memory]\n" + "\n".join(canonical_lines)
            rendered_parts = [part for part in (managed.rendered.strip(), canonical_block) if part]
            rendered = "\n\n".join(rendered_parts)
            if len(rendered) > total_limit:
                rendered = rendered[: max(0, total_limit - 1)].rstrip() + "…"

            memory_version = await self.store.namespace_version(value.user_id, value.workspace)
            recovered = await self.store.latest_checkpoint(
                value.user_id, value.workspace, value.task_key
            )
            items = [*managed.items[:50], *canonical_items[:50]][:100]
            context_events: list[dict[str, Any]] = []
            if items:
                context_events.append(
                    {
                        "user_id": value.user_id,
                        "workspace": value.workspace,
                        "event_type": "recall",
                        "reason": "compiled by embedded memory core before reasoning",
                        "payload": {
                            "context_id": context_id,
                            "items": items[:50],
                            "context_chars": len(rendered),
                            "item_count": len(items),
                        },
                    }
                )
            context_events.append(
                {
                    "user_id": value.user_id,
                    "workspace": value.workspace,
                    "event_type": "context_prepared",
                    "reason": "memory gate prepared context before reasoning",
                    "payload": {
                        "context_id": context_id,
                        "memory_version": memory_version,
                        "task_key_hash": hashlib.sha256(value.task_key.encode("utf-8")).hexdigest()[
                            :16
                        ]
                        if value.task_key
                        else None,
                        "candidate_count": len(canonical_results),
                        "injected_count": len(items),
                        "compiled_chars": len(rendered),
                        "recovered_checkpoint_id": recovered["checkpoint_id"]
                        if recovered
                        else None,
                    },
                }
            )
            record_events = getattr(self.event_store, "record_events", None)
            if callable(record_events):
                await record_events(context_events)
            else:
                for event in context_events:
                    await self.event_store.record_event(**event)
            if value.task_key:
                await self.checkpoint(
                    CheckpointState(
                        user_id=value.user_id,
                        workspace=value.workspace,
                        task_key=value.task_key,
                        stage="context_prepared",
                        state={"context_id": context_id, "injected_count": len(items)},
                        memory_version=memory_version,
                    )
                )
            return MemoryContextBundle(
                context_id=context_id,
                status="ready",
                memory_version=memory_version,
                rendered=rendered,
                items=items,
                checkpoint_id=recovered["checkpoint_id"] if recovered else None,
                candidate_count=len(canonical_results),
                compiled_chars=len(rendered),
            )
        except Exception as exc:
            try:
                await self.event_store.record_event(
                    user_id=value.user_id,
                    workspace=value.workspace,
                    event_type="gate_failed",
                    reason="memory gate could not establish context",
                    trust_level="verified_system_fact",
                    confidence_ppm=1_000_000,
                    payload={
                        "error_type": type(exc).__name__,
                        "required": required,
                        "task_key_present": bool(value.task_key),
                    },
                )
            except Exception:
                pass
            if required:
                raise MemoryUnavailableError(
                    "CPTR memory gate failed; execution is blocked"
                ) from exc
            return MemoryContextBundle(
                context_id=context_id,
                status="degraded",
                memory_version=await self.store.namespace_version(value.user_id, value.workspace),
                rendered="",
            )

    async def record_event(self, **event) -> str:
        payload = redact_sensitive(event.pop("payload", {}) or {})
        user_id = str(event.get("user_id") or "")
        workspace = str(event.get("workspace") or "")
        event_type = str(event.get("event_type") or "unknown")
        row = await self.event_store.record_event(**event, payload=payload)
        if user_id and event_type in _MUTATION_EVENTS:
            await self.store.bump_version(user_id, workspace)
        return row.id

    async def checkpoint(self, value: CheckpointState) -> Checkpoint:
        return await self.store.save_checkpoint(
            user_id=value.user_id,
            workspace=value.workspace,
            task_key=value.task_key,
            stage=value.stage,
            state=value.state,
            memory_version=value.memory_version,
        )

    async def queue_consolidation(
        self,
        *,
        user_id: str,
        workspace: str,
        scope: str,
        text: str,
        heading: str = "",
        kind: str | None = None,
        structured_value: dict[str, Any] | None = None,
        source_event_ids: list[str] | None = None,
        trust_level: str = "agent_observation",
        confidence: float = 0.85,
        importance: float = 0.5,
    ) -> str:
        safe_text = redact_text(text).strip()
        if not safe_text:
            raise ValueError("memory consolidation text must not be blank")
        job_id = await self.job_store.enqueue(
            user_id=user_id,
            workspace=workspace,
            job_type="consolidate",
            payload={
                "scope": scope,
                "text": safe_text,
                "heading": redact_text(heading)[:500],
                "kind": kind,
                "structured_value": redact_sensitive(structured_value or {}),
                "source_event_ids": list(dict.fromkeys(source_event_ids or []))[:200],
                "trust_level": trust_level,
                "confidence": _clamp(confidence, 0.85),
                "importance": _clamp(importance, 0.5),
            },
        )
        await self.event_store.record_event(
            user_id=user_id,
            workspace=workspace,
            event_type="consolidation_queued",
            scope=scope,
            heading=heading or None,
            reason="memory mutation queued for asynchronous consolidation",
            payload={"job_id": job_id, "source_event_count": len(source_event_ids or [])},
        )
        return job_id

    async def project_graph(
        self,
        *,
        user_id: str,
        workspace: str,
        memory_id: str,
        heading: str = "",
    ) -> dict[str, Any]:
        row = await self.store.get_memory(memory_id)
        if row["user_id"] != user_id or row["workspace"] != str(workspace or ""):
            raise KeyError("memory not found")
        projected = await self.graph_store.project_memory(
            user_id=user_id,
            workspace=workspace,
            memory_id=memory_id,
            heading=heading or str(row.get("kind") or "Memory"),
            text=str(row.get("canonical_text") or ""),
            valid_from_ms=row.get("valid_from_ms"),
        )
        await self.event_store.record_event(
            user_id=user_id,
            workspace=workspace,
            event_type="graph_projected",
            memory_id=memory_id,
            reason="derived entity graph updated from canonical memory",
            payload={
                "entity_count": len(projected.get("entity_ids") or []),
                "relationship_count": len(projected.get("relationship_ids") or []),
            },
        )
        return projected

    async def consolidate(self, value: ConsolidationInput) -> MemoryRecordRef:
        text = redact_text(value.text).strip()
        if not text:
            raise ValueError("memory consolidation text must not be blank")
        restore_scope = await self.store.active_restore_scope(value.user_id, value.workspace)
        branch_id = value.branch_id or restore_scope.get("active_branch_id")
        digest = content_hash(text)
        existing = await self.store.find_active_by_hash(
            value.user_id,
            value.workspace,
            digest,
            branch_id=branch_id,
        )
        if existing is not None:
            ref = await self.store.merge_source_events(
                existing["memory_id"], value.source_event_ids
            )
            await self.event_store.record_event(
                user_id=value.user_id,
                workspace=value.workspace,
                event_type="memory_deduplicated",
                memory_id=ref.memory_id,
                reason="consolidation matched canonical content hash",
                payload={"source_event_count": len(value.source_event_ids)},
            )
            return ref

        settings = await self._settings_loader()
        ttl_seconds = int(
            value.verification_ttl_seconds
            if value.verification_ttl_seconds is not None
            else settings.get("verification_ttl_seconds") or 7 * 86400
        )
        now = _now_ms()
        kind = _infer_kind(text, value.heading, value.kind)
        ref = await self.store.create_memory(
            user_id=value.user_id,
            workspace=value.workspace,
            scope=value.scope,
            kind=kind,
            canonical_text=text,
            structured_value=value.structured_value,
            confidence_ppm=int(_clamp(value.confidence, 0.85) * 1_000_000),
            importance_ppm=int(_clamp(value.importance, 0.5) * 1_000_000),
            trust_level=value.trust_level,
            valid_from_ms=value.valid_from_ms if value.valid_from_ms is not None else now,
            source_event_ids=value.source_event_ids,
            branch_id=branch_id,
            verification_expires_at_ms=now + max(0, ttl_seconds) * 1000
            if ttl_seconds > 0
            else None,
        )
        await self.event_store.record_event(
            user_id=value.user_id,
            workspace=value.workspace,
            event_type="memory_created",
            scope=value.scope,
            memory_id=ref.memory_id,
            heading=value.heading or None,
            reason="consolidated into canonical memory",
            trust_level=value.trust_level,
            confidence_ppm=int(_clamp(value.confidence, 0.85) * 1_000_000),
            payload={"kind": kind, "source_event_count": len(value.source_event_ids)},
        )
        return ref

    async def search(self, value: MemoryQuery) -> list[MemoryResult]:
        now_ms = int(value.now_ms if value.now_ms is not None else _now_ms())
        restore_scope = await self.store.active_restore_scope(value.user_id, value.workspace)
        effective_branch = value.branch_id or restore_scope.get("active_branch_id")
        rows = await self.store.list_candidates(
            user_id=value.user_id,
            workspace=value.workspace,
            include_historical=value.include_historical,
            scope=value.scope,
            kinds=value.kinds,
            branch_id=effective_branch,
            limit=max(100, value.limit * 30),
        )
        snapshot_ids = set(restore_scope.get("snapshot_memory_ids") or [])
        active_snapshot = restore_scope.get("active_snapshot_id")
        if active_snapshot and value.branch_id is None:
            rows = [
                row
                for row in rows
                if row.get("branch_id") == effective_branch or row.get("memory_id") in snapshot_ids
            ]
        memory_ids = [str(row["memory_id"]) for row in rows]
        vector_scores: dict[str, float] = {}
        bm25_scores: dict[str, float] = {}
        graph_scores: dict[str, float] = {}
        try:
            vector_scores = dict(
                await self._vector_search.score(
                    user_id=value.user_id,
                    workspace=value.workspace,
                    query=value.query,
                    memory_ids=memory_ids,
                )
            )
            self._index_errors.pop("vector", None)
        except Exception as exc:
            self._index_errors["vector"] = {
                "error_type": type(exc).__name__,
                "at_ms": _now_ms(),
            }
        try:
            bm25_scores = dict(
                await self._lexical_search.score(
                    user_id=value.user_id,
                    workspace=value.workspace,
                    query=value.query,
                    memory_ids=memory_ids,
                )
            )
            self._index_errors.pop("lexical", None)
        except Exception as exc:
            self._index_errors["lexical"] = {
                "error_type": type(exc).__name__,
                "at_ms": _now_ms(),
            }
        try:
            graph_scores = dict(
                await self.graph_store.related_memory_scores(
                    user_id=value.user_id,
                    workspace=value.workspace,
                    query=value.query,
                    limit=max(100, value.limit * 30),
                )
            )
            self._index_errors.pop("graph", None)
        except Exception as exc:
            self._index_errors["graph"] = {
                "error_type": type(exc).__name__,
                "at_ms": _now_ms(),
            }
        profile = await self.store.get_retrieval_profile(value.user_id, value.workspace)
        intelligence = await self.intelligence_store.metrics(memory_ids)
        ranked: list[MemoryResult] = []
        for row in rows:
            memory_id = str(row["memory_id"])
            score, reason, verification_stale, features = score_candidate(
                row,
                query=value.query,
                now_ms=now_ms,
                vector_score=float(vector_scores.get(memory_id, 0.0)),
                bm25_score=float(bm25_scores.get(memory_id, 0.0)),
                graph_score=float(graph_scores.get(memory_id, 0.0)),
                weights=profile["weights"],
                intelligence=intelligence.get(memory_id, {}),
            )
            ranked.append(
                MemoryResult(
                    memory_id=memory_id,
                    scope=str(row["scope"]),
                    kind=str(row["kind"]),
                    canonical_text=str(row["canonical_text"]),
                    score=score,
                    reason=reason,
                    confidence=int(row["confidence_ppm"]) / 1_000_000,
                    importance=int(row["importance_ppm"]) / 1_000_000,
                    trust_level=str(row["trust_level"]),
                    status=str(row["status"]),
                    verification_stale=verification_stale,
                    valid_from_ms=row.get("valid_from_ms"),
                    valid_until_ms=row.get("valid_until_ms"),
                    branch_id=row.get("branch_id"),
                    features=features,
                )
            )
        ranked.sort(key=lambda row: (row.score, row.importance, row.confidence), reverse=True)
        return ranked[: max(1, min(int(value.limit), 100))]

    async def verify(self, memory_id: str, *, user_id: str, workspace: str) -> VerificationResult:
        settings = await self._settings_loader()
        now = _now_ms()
        ttl_seconds = int(settings.get("verification_ttl_seconds") or 7 * 86400)
        existing = await self.store.get_memory(memory_id)
        confidence = max(0.9, int(existing["confidence_ppm"]) / 1_000_000)
        expires = now + ttl_seconds * 1000 if ttl_seconds > 0 else None
        row = await self.store.verify_memory(
            memory_id=memory_id,
            user_id=user_id,
            workspace=workspace,
            verified_at_ms=now,
            verification_expires_at_ms=expires,
            confidence_ppm=int(confidence * 1_000_000),
        )
        await self.event_store.record_event(
            user_id=user_id,
            workspace=workspace,
            event_type="memory_verified",
            memory_id=memory_id,
            reason="memory fact reverified",
            confidence_ppm=int(row["confidence_ppm"]),
            payload={"verification_expires_at_ms": expires},
        )
        return VerificationResult(memory_id, now, expires, int(row["confidence_ppm"]) / 1_000_000)

    async def supersede(
        self, old_memory_id: str, replacement: MemoryReplacement
    ) -> MemoryRecordRef:
        settings = await self._settings_loader()
        now = int(replacement.valid_from_ms if replacement.valid_from_ms is not None else _now_ms())
        ttl = int(
            replacement.verification_ttl_seconds
            if replacement.verification_ttl_seconds is not None
            else settings.get("verification_ttl_seconds") or 7 * 86400
        )
        ref = await self.store.supersede_memory(
            old_memory_id=old_memory_id,
            user_id=replacement.user_id,
            workspace=replacement.workspace,
            scope=replacement.scope,
            kind=replacement.kind,
            canonical_text=replacement.canonical_text,
            structured_value=replacement.structured_value,
            source_event_ids=replacement.source_event_ids,
            trust_level=replacement.trust_level,
            confidence_ppm=int(_clamp(replacement.confidence, 0.9) * 1_000_000),
            importance_ppm=int(_clamp(replacement.importance, 0.5) * 1_000_000),
            valid_from_ms=now,
            verification_expires_at_ms=now + ttl * 1000 if ttl > 0 else None,
            branch_id=replacement.branch_id,
        )
        await self.event_store.record_event(
            user_id=replacement.user_id,
            workspace=replacement.workspace,
            event_type="memory_superseded",
            scope=replacement.scope,
            memory_id=ref.memory_id,
            reason="new temporal fact superseded prior memory",
            trust_level=replacement.trust_level,
            payload={"supersedes": old_memory_id, "kind": replacement.kind},
        )
        await self.job_store.enqueue(
            user_id=replacement.user_id,
            workspace=replacement.workspace,
            job_type="index_memory",
            payload={"memory_id": ref.memory_id, "heading": replacement.kind},
        )
        return ref

    async def feedback(self, value: RetrievalFeedback) -> None:
        await self.store.record_feedback(
            user_id=value.user_id,
            workspace=value.workspace,
            memory_id=value.memory_id,
            context_id=value.context_id,
            query_hash=hashlib.sha256(value.query.encode("utf-8")).hexdigest(),
            rank=value.rank,
            score_ppm=int(_clamp(value.score, 0.0) * 1_000_000),
            used=value.used,
            helpful=value.helpful,
            outcome=value.outcome,
            features=value.features,
        )
        normalized_outcome = str(value.outcome or "").strip().lower()
        positive = value.helpful is True or normalized_outcome in {
            "success",
            "passed",
            "complete",
            "helpful",
        }
        negative = value.helpful is False or normalized_outcome in {
            "failure",
            "failed",
            "error",
            "unhelpful",
        }
        if value.features and (positive or negative):
            await self.store.learn_retrieval_profile(
                value.user_id,
                value.workspace,
                features=value.features,
                positive=positive and not negative,
            )
        if normalized_outcome:
            causal_signal = await self.intelligence_store.record_outcome(
                value.memory_id,
                outcome=normalized_outcome,
            )
            if causal_signal:
                await self.graph_store.apply_causal_interference(
                    user_id=value.user_id,
                    workspace=value.workspace,
                    causal_signals=[causal_signal],
                )

    async def inspect(self, memory_id: str, *, user_id: str, workspace: str) -> dict[str, Any]:
        row = await self.store.get_memory(memory_id)
        if row["user_id"] != user_id or row["workspace"] not in {"", str(workspace or "")}:
            raise KeyError("memory not found")
        return row

    async def index_memory(self, memory_id: str, *, user_id: str, workspace: str) -> None:
        row = await self.inspect(memory_id, user_id=user_id, workspace=workspace)
        await self._lexical_search.index_memory(row)
        index_method = getattr(self._vector_search, "index_memory", None)
        if callable(index_method):
            await index_method(row)
        await self.intelligence_store.project(row)
        await self.event_store.record_event(
            user_id=user_id,
            workspace=workspace,
            event_type="index_updated",
            memory_id=memory_id,
            reason="rebuildable BM25, vector, and intelligence indexes updated",
            payload={
                "lexical_backend": self._lexical_search.sanitized_config().get("backend"),
                "vector_backend": getattr(
                    self._vector_search, "sanitized_config", lambda: {}
                )().get("backend"),
            },
        )

    async def analyze_conflicts(self, memory_id: str) -> list[MemoryConflictRef]:
        row = await self.store.get_memory(memory_id)
        signature = fact_signature(row)
        if signature is None or row["status"] != "active":
            return []
        fact_key, fact_value = signature
        candidates = await self.store.list_candidates(
            user_id=row["user_id"],
            workspace=row["workspace"],
            include_historical=False,
            scope=row["scope"],
            branch_id=row.get("branch_id"),
            limit=1000,
        )
        conflicts: list[MemoryConflictRef] = []
        for candidate in candidates:
            if candidate["memory_id"] == memory_id:
                continue
            candidate_signature = fact_signature(candidate)
            if candidate_signature is None:
                continue
            candidate_key, candidate_value = candidate_signature
            if candidate_key != fact_key or candidate_value == fact_value:
                continue
            classification, confidence, reason = conflict_classification(candidate, row)
            ref = await self.conflict_store.record(
                user_id=row["user_id"],
                workspace=row["workspace"],
                fact_key=fact_key,
                left_memory_id=candidate["memory_id"],
                right_memory_id=memory_id,
                classification=classification,
                confidence=confidence,
                reason=reason,
            )
            conflicts.append(ref)
        if conflicts:
            await self.event_store.record_event(
                user_id=row["user_id"],
                workspace=row["workspace"],
                event_type="conflict_detected",
                memory_id=memory_id,
                reason="active canonical facts disagree on the same fact key",
                payload={"conflict_count": len(conflicts)},
            )
        return conflicts

    async def list_conflicts(
        self,
        *,
        user_id: str,
        workspace: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self.conflict_store.list(
            user_id=user_id,
            workspace=workspace,
            status=status,
            limit=limit,
        )

    async def resolve_conflict(self, conflict_id: str, *, resolution: str) -> dict[str, Any]:
        allowed = {"temporal_change", "prefer_new", "prefer_old", "keep_both"}
        if resolution not in allowed:
            raise ValueError("unsupported conflict resolution")
        # The opaque conflict ID identifies a durable owner-scoped record; callers cannot supply identity.
        conflict = await self.conflict_store.lookup(conflict_id)
        user_id = str(conflict["user_id"])
        workspace = str(conflict["workspace"])
        if resolution in {"temporal_change", "prefer_new"}:
            await self.store.supersede_with_existing(
                old_memory_id=conflict["left_memory_id"],
                new_memory_id=conflict["right_memory_id"],
                user_id=user_id,
                workspace=workspace,
            )
        elif resolution == "prefer_old":
            await self.store.supersede_with_existing(
                old_memory_id=conflict["right_memory_id"],
                new_memory_id=conflict["left_memory_id"],
                user_id=user_id,
                workspace=workspace,
            )
        resolved = await self.conflict_store.resolve(
            conflict_id,
            user_id=user_id,
            resolution=resolution,
        )
        await self.event_store.record_event(
            user_id=user_id,
            workspace=workspace,
            event_type="conflict_resolved",
            reason="memory conflict resolved without deleting historical evidence",
            payload={"conflict_id": conflict_id, "resolution": resolution},
        )
        return resolved

    async def time_travel(
        self,
        user_id: str,
        workspace: str,
        *,
        at_ms: int,
        known_at_ms: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return await self.store.time_travel_records(
            user_id,
            workspace,
            at_ms=at_ms,
            known_at_ms=known_at_ms,
            limit=limit,
        )

    async def compare_snapshots(
        self,
        user_id: str,
        workspace: str,
        left_snapshot_id: str,
        right_snapshot_id: str,
    ) -> dict[str, Any]:
        return await self.store.compare_snapshots(
            user_id,
            workspace,
            left_snapshot_id,
            right_snapshot_id,
        )

    async def merge_branch(
        self,
        user_id: str,
        workspace: str,
        branch_id: str,
        *,
        strategy: str = "verified_only",
    ) -> dict[str, Any]:
        result = await self.store.merge_branch(
            user_id,
            workspace,
            branch_id,
            strategy=strategy,
        )
        for memory_id in result["merged_memory_ids"]:
            await self.job_store.enqueue(
                user_id=user_id,
                workspace=workspace,
                job_type="index_memory",
                payload={"memory_id": memory_id},
            )
        await self.event_store.record_event(
            user_id=user_id,
            workspace=workspace,
            event_type="branch_merged",
            reason="verified branch knowledge merged into canonical memory",
            payload={
                "branch_id": branch_id,
                "strategy": strategy,
                "merged_count": result["merged_count"],
                "skipped_count": result["skipped_count"],
                "conflicted_count": result.get("conflicted_count", 0),
            },
        )
        return result

    async def forget(
        self,
        memory_id: str,
        *,
        user_id: str,
        workspace: str,
        source_forgetter: SourceForgetter | None = None,
    ) -> bool:
        row = await self.inspect(memory_id, user_id=user_id, workspace=workspace)
        actual_workspace = str(row.get("workspace") or "")
        structured = (
            row.get("structured_value") if isinstance(row.get("structured_value"), dict) else {}
        )
        managed_source = structured.get("managed_source") if isinstance(structured, dict) else None
        if isinstance(managed_source, dict) and managed_source and source_forgetter is None:
            raise MemoryUnavailableError(
                "managed memory source requires an authenticated source-forget capability"
            )

        await self.store.begin_forget(
            memory_id,
            user_id=user_id,
            workspace=actual_workspace,
        )
        if source_forgetter is not None and isinstance(managed_source, dict) and managed_source:
            try:
                await source_forgetter(row)
            except Exception as exc:
                raise MemoryUnavailableError(
                    "managed memory source deletion failed; memory remains non-recallable and retryable"
                ) from exc

        vector_remove = getattr(self._vector_search, "remove_memory", None)
        if callable(vector_remove):
            await vector_remove(
                memory_id,
                user_id=user_id,
                workspace=actual_workspace,
            )
        await self._lexical_search.remove_memory(
            memory_id,
            user_id=user_id,
            workspace=actual_workspace,
        )
        await self.graph_store.remove_memory(
            user_id=user_id,
            workspace=actual_workspace,
            memory_id=memory_id,
        )
        await self.store.forget_memory(
            memory_id,
            user_id=user_id,
            workspace=actual_workspace,
        )
        await self.event_store.record_event(
            user_id=user_id,
            workspace=actual_workspace,
            event_type="memory_forgotten",
            scope=str(row.get("scope") or "workspace"),
            memory_id=memory_id,
            reason="explicit owner-authorized memory deletion",
            trust_level="user_directive",
            confidence_ppm=1_000_000,
            payload={"kind": str(row.get("kind") or "semantic")},
        )
        return True

    async def rebuild_derived_indexes(
        self,
        user_id: str,
        workspace: str,
        *,
        batch_size: int = 200,
    ) -> dict[str, Any]:
        safe_batch = max(1, min(int(batch_size), 1000))
        await self._lexical_search.clear_scope(user_id=user_id, workspace=workspace)
        vector_clear = getattr(self._vector_search, "clear_scope", None)
        if callable(vector_clear):
            await vector_clear(user_id=user_id, workspace=workspace)
        await self.graph_store.clear_scope(user_id=user_id, workspace=workspace)
        await self.intelligence_store.clear_scope(user_id=user_id, workspace=workspace)
        await self.conflict_store.clear_scope(user_id=user_id, workspace=workspace)
        offset = 0
        scanned = 0
        indexed = 0
        memory_ids: list[str] = []
        while True:
            rows = await self.store.list_records_page(
                user_id,
                workspace,
                offset=offset,
                limit=safe_batch,
            )
            if not rows:
                break
            for row in rows:
                scanned += 1
                memory_id = str(row.get("memory_id") or "")
                if not memory_id or str(row.get("status") or "") == "deleting":
                    continue
                actual_workspace = str(row.get("workspace") or "")
                await self.graph_store.project_memory(
                    user_id=user_id,
                    workspace=actual_workspace,
                    memory_id=memory_id,
                    heading=str(row.get("kind") or "Memory"),
                    text=str(row.get("canonical_text") or ""),
                    valid_from_ms=row.get("valid_from_ms"),
                )
                await self._lexical_search.index_memory(row)
                vector_index = getattr(self._vector_search, "index_memory", None)
                if callable(vector_index):
                    await vector_index(row)
                await self.intelligence_store.project(row)
                if str(row.get("status") or "") == "active":
                    await self.analyze_conflicts(memory_id)
                indexed += 1
                if len(memory_ids) < 1000:
                    memory_ids.append(memory_id)
            offset += len(rows)
            if len(rows) < safe_batch:
                break
        await self.event_store.record_event(
            user_id=user_id,
            workspace=workspace,
            event_type="indexes_rebuilt",
            reason="derived memory indexes rebuilt from canonical truth",
            trust_level="verified_system_fact",
            confidence_ppm=1_000_000,
            payload={"scanned": scanned, "indexed": indexed},
        )
        return {"scanned": scanned, "indexed": indexed, "memory_ids": memory_ids}

    async def queue_rebuild(self, user_id: str, workspace: str) -> str:
        job_id = await self.job_store.enqueue(
            user_id=user_id,
            workspace=workspace,
            job_type="rebuild_indexes",
            payload={},
        )
        await self.event_store.record_event(
            user_id=user_id,
            workspace=workspace,
            event_type="index_rebuild_queued",
            reason="derived index rebuild queued from canonical truth",
            payload={"job_id": job_id},
        )
        return job_id

    async def health(self, *, user_id: str, workspace: str | None) -> dict[str, Any]:
        vector_config = getattr(
            self._vector_search, "sanitized_config", lambda: {"backend": "custom"}
        )()
        vector_coverage = 0
        lexical_coverage = 0
        try:
            vector_coverage = int(
                await getattr(self._vector_search, "coverage")(user_id=user_id, workspace=workspace)
            )
        except Exception as exc:
            self._index_errors["vector"] = {"error_type": type(exc).__name__, "at_ms": _now_ms()}
        try:
            lexical_coverage = int(
                await self._lexical_search.coverage(user_id=user_id, workspace=workspace)
            )
        except Exception as exc:
            self._index_errors["lexical"] = {"error_type": type(exc).__name__, "at_ms": _now_ms()}
        conflicts = await self.conflict_store.list(
            user_id=user_id,
            workspace=workspace,
            status="open",
            limit=500,
        )
        intelligence = await self.intelligence_store.counts(
            user_id=user_id,
            workspace=workspace,
        )
        profile = await self.store.get_retrieval_profile(user_id, str(workspace or ""))
        jobs = await self.job_store.counts(user_id=user_id, workspace=workspace)
        return {
            "status": "degraded" if self._index_errors else "healthy",
            "canonical_store": "embedded-sql-versioned",
            "vector": {**vector_config, "coverage": vector_coverage},
            "lexical": {**self._lexical_search.sanitized_config(), "coverage": lexical_coverage},
            "retrieval_learning": profile,
            "open_conflicts": len(conflicts),
            "intelligence": intelligence,
            "jobs": jobs,
            "index_errors": dict(self._index_errors),
        }

    async def snapshot(self, user_id: str, workspace: str, *, label: str = "") -> SnapshotRef:
        ref = await self.store.create_snapshot(user_id, workspace, label=label)
        await self.event_store.record_event(
            user_id=user_id,
            workspace=workspace,
            event_type="snapshot_created",
            reason="immutable memory snapshot created",
            payload={"snapshot_id": ref.snapshot_id, "memory_version": ref.memory_version},
        )
        return ref

    async def create_branch(
        self,
        user_id: str,
        workspace: str,
        *,
        name: str,
        from_snapshot_id: str | None = None,
    ) -> BranchRef:
        ref = await self.store.create_branch(
            user_id,
            workspace,
            name=name,
            from_snapshot_id=from_snapshot_id,
        )
        await self.event_store.record_event(
            user_id=user_id,
            workspace=workspace,
            event_type="branch_created",
            reason="memory branch created",
            payload={"branch_id": ref.branch_id, "from_snapshot_id": ref.from_snapshot_id},
        )
        return ref

    async def restore_snapshot(self, user_id: str, workspace: str, snapshot_id: str) -> SnapshotRef:
        ref = await self.store.restore_snapshot(user_id, workspace, snapshot_id)
        await self.event_store.record_event(
            user_id=user_id,
            workspace=workspace,
            event_type="snapshot_restored",
            reason="snapshot activated through non-destructive restore branch",
            payload={"snapshot_id": snapshot_id, "memory_version": ref.memory_version},
        )
        return ref


_default_service: EmbeddedMemoryService | None = None


def get_memory_service() -> EmbeddedMemoryService:
    global _default_service
    if _default_service is None:
        _default_service = EmbeddedMemoryService()
    return _default_service


def set_memory_service(service: EmbeddedMemoryService | None) -> None:
    """Test/runtime injection point; CPTR callers still depend on the service port."""
    global _default_service
    _default_service = service
