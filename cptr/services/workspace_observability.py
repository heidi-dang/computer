"""Workspace OS Observability: events, context cache/invalidation, live projections, and metrics.

Reuses existing socket/SSE/telemetry systems with zero duplicate event bus.
Covers invalidation across all 7 axes: instructions, environment, memory,
checkpoint, revision, role, and canonical-checkout changes, plus bounded
context cache behavior with LRU eviction and OpenTelemetry integration.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import logging
import os
import time
from typing import Any

from sqlalchemy import select

from cptr.events import EVENTS
from cptr.models.workspaces import (
    Repository,
    RepositoryCheckout,
    Workspace,
    WorkspaceRepository,
)
from cptr.models.workspace_context import (
    WorkspaceContextSnapshot,
)
from cptr.services.live_events import (
    LiveEventEnvelope,
    LiveEventHub,
    live_event_hub,
    workspace_target_key,
)
from cptr.services.telemetry import CptrTelemetry
from cptr.utils.db import get_db

logger = logging.getLogger(__name__)

MAX_RECENT_EVENTS_PER_WORKSPACE = 32
DEFAULT_CONTEXT_CACHE_MAX_ENTRIES = 128
DEFAULT_CONTEXT_CACHE_TTL_SECONDS = 300.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_digest(data: Any) -> str:
    try:
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        raw = str(data)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ContextInvalidationReason(str, Enum):
    """Canonical invalidation triggers for Workspace OS context cache."""

    INSTRUCTIONS = "instructions"
    ENVIRONMENT = "environment"
    MEMORY = "memory"
    CHECKPOINT = "checkpoint"
    REVISION = "revision"
    ROLE = "role"
    CANONICAL_CHECKOUT = "canonical_checkout"
    TTL_EXPIRED = "ttl_expired"
    CAPACITY_EVICTION = "capacity_eviction"
    MANUAL = "manual"
    WORKSPACE_DELETED = "workspace_deleted"


@dataclass
class ContextValidationTokens:
    """Validator tokens representing the 7 invalidation axes of workspace context."""

    instructions: str | None = None
    environment: str | None = None
    memory: str | None = None
    checkpoint: str | None = None
    revision: str | None = None
    role: str | None = None
    canonical_checkout: str | None = None
    specified_axes: set[str] | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            k: v
            for k, v in {
                "instructions": self.instructions,
                "environment": self.environment,
                "memory": self.memory,
                "checkpoint": self.checkpoint,
                "revision": self.revision,
                "role": self.role,
                "canonical_checkout": self.canonical_checkout,
            }.items()
            if v is not None
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ContextValidationTokens:
        if not data:
            return cls()
        return cls(
            instructions=str(data["instructions"])
            if data.get("instructions") is not None
            else None,
            environment=str(data["environment"]) if data.get("environment") is not None else None,
            memory=str(data["memory"]) if data.get("memory") is not None else None,
            checkpoint=str(data["checkpoint"]) if data.get("checkpoint") is not None else None,
            revision=str(data["revision"]) if data.get("revision") is not None else None,
            role=str(data["role"]) if data.get("role") is not None else None,
            canonical_checkout=str(data["canonical_checkout"])
            if data.get("canonical_checkout") is not None
            else None,
            specified_axes=set(data.keys()),
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: WorkspaceContextSnapshot | dict[str, Any],
        *,
        role: str | None = None,
        canonical_checkout: str | None = None,
    ) -> ContextValidationTokens:
        """Derive validation tokens from a WorkspaceContextSnapshot or dictionary."""
        specified = {"instructions", "environment", "memory", "checkpoint", "revision"}
        if role is not None:
            specified.add("role")
        if canonical_checkout is not None:
            specified.add("canonical_checkout")

        if isinstance(snapshot, dict):
            instructions_obj = snapshot.get("instructions") or {}
            env_obj = snapshot.get("environment") or {}
            mem_obj = snapshot.get("memory") or {}
            repo_obj = snapshot.get("repo") or {}
            div_obj = snapshot.get("divergence") or {}

            inst_digest = _safe_digest(instructions_obj) if any(instructions_obj.values()) else None
            env_digest = _safe_digest(env_obj) if any(env_obj.values()) else None

            mem_val = (
                str(mem_obj.get("memory_version") or "")
                if mem_obj.get("memory_version") is not None
                else None
            )
            if not mem_val and mem_obj.get("canonical_memories"):
                mem_val = _safe_digest(mem_obj.get("canonical_memories"))

            chk_id = div_obj.get("checkpoint_id")
            chk_rev = div_obj.get("checkpoint_revision")
            chk_val = None
            if chk_id and chk_rev:
                chk_val = f"{chk_id}:{chk_rev}"
            elif chk_id:
                chk_val = str(chk_id)
            elif chk_rev:
                chk_val = str(chk_rev)

            rev_val = str(repo_obj.get("revision") or "") if repo_obj.get("revision") else None
            if rev_val and repo_obj.get("is_dirty"):
                rev_val = f"{rev_val}:dirty"
        else:
            # WorkspaceContextSnapshot dataclass
            inst = snapshot.instructions
            inst_dict = inst.to_dict() if hasattr(inst, "to_dict") else asdict(inst)
            inst_digest = _safe_digest(inst_dict) if any(inst_dict.values()) else None

            env = snapshot.environment
            env_dict = env.to_dict() if hasattr(env, "to_dict") else asdict(env)
            env_digest = _safe_digest(env_dict) if any(env_dict.values()) else None

            mem = snapshot.memory
            mem_val = str(mem.memory_version) if mem.memory_version is not None else None
            if not mem_val and mem.canonical_memories:
                mem_val = _safe_digest(mem.canonical_memories)

            div = snapshot.divergence
            chk_val = None
            if div.checkpoint_id and div.checkpoint_revision:
                chk_val = f"{div.checkpoint_id}:{div.checkpoint_revision}"
            elif div.checkpoint_id:
                chk_val = str(div.checkpoint_id)
            elif div.checkpoint_revision:
                chk_val = str(div.checkpoint_revision)

            repo = snapshot.repo
            rev_val = str(repo.revision) if repo.revision else None
            if rev_val and repo.is_dirty:
                rev_val = f"{rev_val}:dirty"

        return cls(
            instructions=inst_digest,
            environment=env_digest,
            memory=mem_val,
            checkpoint=chk_val,
            revision=rev_val,
            role=role,
            canonical_checkout=canonical_checkout,
            specified_axes=specified,
        )


@dataclass
class ContextCacheEntry:
    """Bounded, inspectable cache entry for Workspace OS compiled context."""

    workspace_id: str
    user_id: str | None
    snapshot: Any
    tokens: ContextValidationTokens
    fingerprint: str
    cached_at_ms: int
    expires_at_ms: int | None
    access_count: int = 0
    last_accessed_at_ms: int = 0

    def is_expired(self, now_ms: int | None = None) -> bool:
        if self.expires_at_ms is None:
            return False
        current = now_ms or _now_ms()
        return current > self.expires_at_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "fingerprint": self.fingerprint,
            "tokens": self.tokens.to_dict(),
            "cached_at_ms": self.cached_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "access_count": self.access_count,
            "last_accessed_at_ms": self.last_accessed_at_ms,
        }


class WorkspaceObservabilityMetrics:
    """In-memory counters and timers for Workspace OS observability with OTel integration."""

    def __init__(self, *, telemetry: CptrTelemetry | None = None) -> None:
        self.telemetry = telemetry or CptrTelemetry()
        self.hits: int = 0
        self.misses: int = 0
        self.evictions: int = 0
        self.invalidations_by_reason: dict[str, int] = {
            r.value: 0 for r in ContextInvalidationReason
        }
        self.events_published: dict[str, int] = {}
        self.projections_generated: int = 0

    def record_hit(self, workspace_id: str | None = None) -> None:
        self.hits += 1

    def record_miss(self, workspace_id: str | None = None) -> None:
        self.misses += 1

    def record_eviction(self, workspace_id: str | None = None) -> None:
        self.evictions += 1

    def record_invalidation(self, reason: str, workspace_id: str | None = None) -> None:
        canonical_reason = str(reason).lower()
        self.invalidations_by_reason[canonical_reason] = (
            self.invalidations_by_reason.get(canonical_reason, 0) + 1
        )
        if self.telemetry.configured:
            try:
                span = self.telemetry.start_span(
                    "workspace.context.invalidation",
                    attributes={
                        "cptr.component": "workspace_os",
                        "cptr.operation": f"context_cache.invalidate.{canonical_reason[:50]}",
                        "cptr.status": "ok",
                    },
                )
                with span:
                    pass
            except Exception:
                pass

    def record_event_published(self, event_type: str) -> None:
        self.events_published[event_type] = self.events_published.get(event_type, 0) + 1

    def record_projection_generated(self) -> None:
        self.projections_generated += 1

    def snapshot(self) -> dict[str, Any]:
        total_inval = sum(self.invalidations_by_reason.values())
        total_requests = self.hits + self.misses
        hit_ratio = (self.hits / total_requests) if total_requests > 0 else 0.0
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_ratio": round(hit_ratio, 4),
            "evictions": self.evictions,
            "total_invalidations": total_inval,
            "invalidations_by_reason": dict(self.invalidations_by_reason),
            "events_published": dict(self.events_published),
            "projections_generated": self.projections_generated,
        }

    def reset(self) -> None:
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.invalidations_by_reason = {r.value: 0 for r in ContextInvalidationReason}
        self.events_published.clear()
        self.projections_generated = 0


class WorkspaceContextCache:
    """Bounded, thread-safe Workspace OS Context Cache with LRU eviction and multi-axis invalidation."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_CONTEXT_CACHE_MAX_ENTRIES,
        default_ttl_seconds: float | None = DEFAULT_CONTEXT_CACHE_TTL_SECONDS,
        metrics: WorkspaceObservabilityMetrics | None = None,
        hub: LiveEventHub | None = None,
    ) -> None:
        self.max_entries = max(1, max_entries)
        self.default_ttl_seconds = default_ttl_seconds
        self.metrics = metrics or WorkspaceObservabilityMetrics()
        self.hub = hub or live_event_hub
        self._entries: OrderedDict[str, ContextCacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._entries)

    def contains(self, workspace_id: str) -> bool:
        entry = self._entries.get(workspace_id)
        if entry is None:
            return False
        if entry.is_expired():
            return False
        return True

    async def get(
        self,
        workspace_id: str,
        *,
        current_tokens: ContextValidationTokens | dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> Any | None:
        """Get cached context snapshot if present and valid; invalidates on token divergence."""
        now = _now_ms()
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is None:
                self.metrics.record_miss(workspace_id)
                return None

            # 1. Check TTL expiry
            if entry.is_expired(now):
                self._entries.pop(workspace_id, None)
                self.metrics.record_miss(workspace_id)
                self.metrics.record_invalidation(
                    ContextInvalidationReason.TTL_EXPIRED.value, workspace_id
                )
                await self._publish_invalidation_event(
                    workspace_id=workspace_id,
                    user_id=user_id or entry.user_id,
                    reason=ContextInvalidationReason.TTL_EXPIRED.value,
                    details={"expired_at_ms": entry.expires_at_ms, "now_ms": now},
                )
                return None

            # 2. Check token validation across the 7 axes if current_tokens supplied
            if current_tokens is not None:
                tokens = (
                    current_tokens
                    if isinstance(current_tokens, ContextValidationTokens)
                    else ContextValidationTokens.from_dict(current_tokens)
                )
                mismatch_axis = self._find_token_mismatch(entry.tokens, tokens)
                if mismatch_axis is not None:
                    # Invalidate due to divergence on this axis
                    self._entries.pop(workspace_id, None)
                    self.metrics.record_miss(workspace_id)
                    self.metrics.record_invalidation(mismatch_axis, workspace_id)
                    await self._publish_invalidation_event(
                        workspace_id=workspace_id,
                        user_id=user_id or entry.user_id,
                        reason=mismatch_axis,
                        details={
                            "axis": mismatch_axis,
                            "previous": getattr(entry.tokens, mismatch_axis, None),
                            "current": getattr(tokens, mismatch_axis, None),
                        },
                    )
                    return None

            # Cache hit: update LRU position and access statistics
            self._entries.move_to_end(workspace_id)
            entry.access_count += 1
            entry.last_accessed_at_ms = now
            self.metrics.record_hit(workspace_id)
            return entry.snapshot

    async def put(
        self,
        workspace_id: str,
        snapshot: Any,
        *,
        tokens: ContextValidationTokens | dict[str, Any] | None = None,
        ttl_seconds: float | None = None,
        user_id: str | None = None,
    ) -> ContextCacheEntry:
        """Put snapshot into bounded cache, evicting oldest entry if full."""
        now = _now_ms()
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        expires_at_ms = (now + int(ttl * 1000)) if ttl and ttl > 0 else None

        if isinstance(tokens, ContextValidationTokens):
            val_tokens = tokens
        elif isinstance(tokens, dict):
            val_tokens = ContextValidationTokens.from_dict(tokens)
        else:
            val_tokens = ContextValidationTokens.from_snapshot(snapshot)

        fingerprint = getattr(snapshot, "content_digest", None) or _safe_digest(snapshot)

        async with self._lock:
            # Check capacity and evict LRU if needed
            if workspace_id not in self._entries and len(self._entries) >= self.max_entries:
                evicted_id, evicted_entry = self._entries.popitem(last=False)
                self.metrics.record_eviction(evicted_id)
                self.metrics.record_invalidation(
                    ContextInvalidationReason.CAPACITY_EVICTION.value, evicted_id
                )
                await self._publish_invalidation_event(
                    workspace_id=evicted_id,
                    user_id=evicted_entry.user_id,
                    reason=ContextInvalidationReason.CAPACITY_EVICTION.value,
                    details={"max_entries": self.max_entries},
                )

            entry = ContextCacheEntry(
                workspace_id=workspace_id,
                user_id=user_id,
                snapshot=snapshot,
                tokens=val_tokens,
                fingerprint=fingerprint,
                cached_at_ms=now,
                expires_at_ms=expires_at_ms,
                access_count=0,
                last_accessed_at_ms=now,
            )
            self._entries[workspace_id] = entry
            self._entries.move_to_end(workspace_id)

        # Publish context updated event
        await self._publish_updated_event(
            workspace_id=workspace_id,
            user_id=user_id,
            entry=entry,
        )
        return entry

    def invalidate_sync(
        self,
        workspace_id: str,
        *,
        reason: str | ContextInvalidationReason = ContextInvalidationReason.MANUAL,
    ) -> bool:
        """Synchronously evict workspace context cache entry."""
        canonical_reason = (
            reason.value if isinstance(reason, ContextInvalidationReason) else str(reason).lower()
        )
        entry = self._entries.pop(workspace_id, None)
        if entry is None:
            return False
        self.metrics.record_invalidation(canonical_reason, workspace_id)
        return True

    async def invalidate(
        self,
        workspace_id: str,
        *,
        reason: str | ContextInvalidationReason = ContextInvalidationReason.MANUAL,
        details: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> bool:
        """Invalidate single workspace context cache entry."""
        canonical_reason = (
            reason.value if isinstance(reason, ContextInvalidationReason) else str(reason).lower()
        )
        async with self._lock:
            entry = self._entries.pop(workspace_id, None)
            if entry is None:
                return False

        self.metrics.record_invalidation(canonical_reason, workspace_id)
        await self._publish_invalidation_event(
            workspace_id=workspace_id,
            user_id=user_id or entry.user_id,
            reason=canonical_reason,
            details=details,
        )
        return True

    # 7 Dedicated Invalidation Handlers matching required axes
    async def invalidate_on_instructions_change(
        self,
        workspace_id: str,
        *,
        details: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> bool:
        return await self.invalidate(
            workspace_id,
            reason=ContextInvalidationReason.INSTRUCTIONS,
            details=details,
            user_id=user_id,
        )

    async def invalidate_on_environment_change(
        self,
        workspace_id: str,
        *,
        details: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> bool:
        return await self.invalidate(
            workspace_id,
            reason=ContextInvalidationReason.ENVIRONMENT,
            details=details,
            user_id=user_id,
        )

    async def invalidate_on_memory_change(
        self,
        workspace_id: str,
        *,
        details: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> bool:
        return await self.invalidate(
            workspace_id,
            reason=ContextInvalidationReason.MEMORY,
            details=details,
            user_id=user_id,
        )

    async def invalidate_on_checkpoint_change(
        self,
        workspace_id: str,
        *,
        details: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> bool:
        return await self.invalidate(
            workspace_id,
            reason=ContextInvalidationReason.CHECKPOINT,
            details=details,
            user_id=user_id,
        )

    async def invalidate_on_revision_change(
        self,
        workspace_id: str,
        *,
        details: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> bool:
        return await self.invalidate(
            workspace_id,
            reason=ContextInvalidationReason.REVISION,
            details=details,
            user_id=user_id,
        )

    async def invalidate_on_role_change(
        self,
        workspace_id: str,
        *,
        details: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> bool:
        return await self.invalidate(
            workspace_id,
            reason=ContextInvalidationReason.ROLE,
            details=details,
            user_id=user_id,
        )

    async def invalidate_on_canonical_checkout_change(
        self,
        workspace_id: str,
        *,
        details: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> bool:
        return await self.invalidate(
            workspace_id,
            reason=ContextInvalidationReason.CANONICAL_CHECKOUT,
            details=details,
            user_id=user_id,
        )

    async def invalidate_all(
        self,
        *,
        reason: str | ContextInvalidationReason = ContextInvalidationReason.MANUAL,
        user_id: str | None = None,
    ) -> int:
        """Invalidate all cached workspace entries."""
        canonical_reason = (
            reason.value if isinstance(reason, ContextInvalidationReason) else str(reason).lower()
        )
        async with self._lock:
            items = list(self._entries.items())
            self._entries.clear()

        for ws_id, entry in items:
            self.metrics.record_invalidation(canonical_reason, ws_id)
            await self._publish_invalidation_event(
                workspace_id=ws_id,
                user_id=user_id or entry.user_id,
                reason=canonical_reason,
                details={"scope": "all"},
            )
        return len(items)

    async def invalidate_for_repository(
        self,
        repository_id: str,
        *,
        reason: str | ContextInvalidationReason = ContextInvalidationReason.REVISION,
        details: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> int:
        """Invalidate all cached workspaces that contain the given repository."""
        async with await get_db() as db:
            result = await db.scalars(
                select(WorkspaceRepository.workspace_id).where(
                    WorkspaceRepository.repository_id == repository_id
                )
            )
            workspace_ids = set(result.all())

        count = 0
        for ws_id in workspace_ids:
            if await self.invalidate(
                ws_id,
                reason=reason,
                details={"repository_id": repository_id, **(details or {})},
                user_id=user_id,
            ):
                count += 1
        return count

    def get_entry(self, workspace_id: str) -> ContextCacheEntry | None:
        entry = self._entries.get(workspace_id)
        if entry and entry.is_expired():
            return None
        return entry

    def stats(self) -> dict[str, Any]:
        now = _now_ms()
        valid_count = sum(1 for e in self._entries.values() if not e.is_expired(now))
        return {
            "size": valid_count,
            "raw_size": len(self._entries),
            "max_entries": self.max_entries,
            "default_ttl_seconds": self.default_ttl_seconds,
            "metrics": self.metrics.snapshot(),
            "cached_workspaces": [
                {
                    "workspace_id": e.workspace_id,
                    "fingerprint": e.fingerprint,
                    "cached_at_ms": e.cached_at_ms,
                    "access_count": e.access_count,
                    "expired": e.is_expired(now),
                }
                for e in self._entries.values()
            ],
        }

    def _find_token_mismatch(
        self,
        cached: ContextValidationTokens,
        current: ContextValidationTokens,
    ) -> str | None:
        """Returns the first differing invalidation axis, or None if matching."""
        axes = (
            ContextInvalidationReason.INSTRUCTIONS.value,
            ContextInvalidationReason.ENVIRONMENT.value,
            ContextInvalidationReason.MEMORY.value,
            ContextInvalidationReason.CHECKPOINT.value,
            ContextInvalidationReason.REVISION.value,
            ContextInvalidationReason.ROLE.value,
            ContextInvalidationReason.CANONICAL_CHECKOUT.value,
        )
        for axis in axes:
            if current.specified_axes is not None and axis not in current.specified_axes:
                continue
            cur_val = getattr(current, axis, None)
            cached_val = getattr(cached, axis, None)
            if current.specified_axes is None and cur_val is None:
                continue
            if cur_val != cached_val:
                return axis
        return None

    async def _publish_invalidation_event(
        self,
        *,
        workspace_id: str,
        user_id: str | None,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        event_type = EVENTS.WORKSPACE_CONTEXT_INVALIDATED.name
        self.metrics.record_event_published(event_type)
        try:
            await self.hub.publish(
                user_id=user_id or "system",
                target_key=workspace_target_key(workspace_id),
                event_type=event_type,
                payload={
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "details": details or {},
                    "invalidated_at_ms": _now_ms(),
                },
            )
        except Exception:
            logger.debug("workspace context invalidation event publication failed", exc_info=True)

    async def _publish_updated_event(
        self,
        *,
        workspace_id: str,
        user_id: str | None,
        entry: ContextCacheEntry,
    ) -> None:
        event_type = EVENTS.WORKSPACE_CONTEXT_UPDATED.name
        self.metrics.record_event_published(event_type)
        try:
            await self.hub.publish(
                user_id=user_id or "system",
                target_key=workspace_target_key(workspace_id),
                event_type=event_type,
                payload={
                    "workspace_id": workspace_id,
                    "fingerprint": entry.fingerprint,
                    "cached_at_ms": entry.cached_at_ms,
                    "expires_at_ms": entry.expires_at_ms,
                    "validation_tokens": entry.tokens.to_dict(),
                },
            )
        except Exception:
            logger.debug("workspace context update event publication failed", exc_info=True)


class WorkspaceLiveProjectionService:
    """Read model projection aggregating workspace state, repository links, context cache, and metrics."""

    def __init__(
        self,
        *,
        cache: WorkspaceContextCache | None = None,
        metrics: WorkspaceObservabilityMetrics | None = None,
        hub: LiveEventHub | None = None,
    ) -> None:
        self.cache = cache or workspace_context_cache
        self.metrics = metrics or (
            self.cache.metrics if self.cache else WorkspaceObservabilityMetrics()
        )
        self.hub = hub or live_event_hub
        self._projections: dict[str, dict[str, Any]] = {}
        self._projection_fingerprints: dict[str, str] = {}
        self._recent_events: dict[str, list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def record_live_event(self, event: LiveEventEnvelope) -> None:
        """Record live event into recent events ring buffer and trigger projection invalidation."""
        target_type, _, remainder = event.target_key.partition(":")
        workspace_id = None
        if target_type == "workspace":
            workspace_id = remainder
        elif isinstance(event.payload, dict) and event.payload.get("workspace_id"):
            workspace_id = str(event.payload.get("workspace_id"))

        if not workspace_id:
            return

        events = self._recent_events.setdefault(workspace_id, [])
        events.append(event.to_dict())
        if len(events) > MAX_RECENT_EVENTS_PER_WORKSPACE:
            self._recent_events[workspace_id] = events[-MAX_RECENT_EVENTS_PER_WORKSPACE:]
        self._projections.pop(workspace_id, None)

        event_to_reason = {
            EVENTS.WORKSPACE_INSTRUCTIONS_CHANGED.name: ContextInvalidationReason.INSTRUCTIONS,
            EVENTS.WORKSPACE_ENVIRONMENT_CHANGED.name: ContextInvalidationReason.ENVIRONMENT,
            EVENTS.WORKSPACE_MEMORY_CHANGED.name: ContextInvalidationReason.MEMORY,
            EVENTS.WORKSPACE_CHECKPOINT_CHANGED.name: ContextInvalidationReason.CHECKPOINT,
            EVENTS.WORKSPACE_REVISION_CHANGED.name: ContextInvalidationReason.REVISION,
            EVENTS.WORKSPACE_ROLE_CHANGED.name: ContextInvalidationReason.ROLE,
            EVENTS.WORKSPACE_CHECKOUT_CHANGED.name: ContextInvalidationReason.CANONICAL_CHECKOUT,
            EVENTS.WORKSPACE_DELETED.name: ContextInvalidationReason.WORKSPACE_DELETED,
            EVENTS.WORKSPACE_UPDATED.name: ContextInvalidationReason.MANUAL,
        }
        reason = event_to_reason.get(event.event_type)
        if reason and self.cache:
            self.cache.invalidate_sync(workspace_id, reason=reason)

    def invalidate_projection(self, workspace_id: str) -> bool:
        """Explicitly invalidate cached live projection for a workspace."""
        removed = self._projections.pop(workspace_id, None)
        return removed is not None

    async def get_projection(
        self,
        workspace_id: str,
        user_id: str,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Produce authoritative live projection for the given workspace."""
        async with self._lock:
            cached = self._projections.get(workspace_id)
            if cached is not None and not force_refresh:
                return cached

        projection = await self._build_projection(workspace_id=workspace_id, user_id=user_id)
        fingerprint = _safe_digest(projection)
        projection["fingerprint"] = fingerprint
        projection["generated_at_ms"] = _now_ms()

        async with self._lock:
            old_fingerprint = self._projection_fingerprints.get(workspace_id)
            self._projections[workspace_id] = projection
            self._projection_fingerprints[workspace_id] = fingerprint

        self.metrics.record_projection_generated()

        # Emit projection updated event if changed
        if old_fingerprint is not None and old_fingerprint != fingerprint:
            event_type = EVENTS.WORKSPACE_PROJECTION_UPDATED.name
            self.metrics.record_event_published(event_type)
            try:
                await self.hub.publish(
                    user_id=user_id,
                    target_key=workspace_target_key(workspace_id),
                    event_type=event_type,
                    payload={
                        "workspace_id": workspace_id,
                        "fingerprint": fingerprint,
                        "status": projection.get("status"),
                    },
                )
            except Exception:
                logger.debug("workspace projection update event publication failed", exc_info=True)

        return projection

    async def _build_projection(self, *, workspace_id: str, user_id: str) -> dict[str, Any]:
        async with await get_db() as db:
            ws_res = await db.execute(
                select(Workspace).where(
                    Workspace.id == workspace_id,
                    Workspace.user_id == user_id,
                )
            )
            workspace = ws_res.scalar_one_or_none()
            if workspace is None:
                raise ValueError(f"Workspace not found: {workspace_id}")

            # Fetch repository links
            repos_res = await db.execute(
                select(WorkspaceRepository, Repository)
                .join(Repository, WorkspaceRepository.repository_id == Repository.id)
                .where(WorkspaceRepository.workspace_id == workspace_id)
                .order_by(WorkspaceRepository.sort_order.asc())
            )
            linked_repos = repos_res.all()

            repo_details = []
            for ws_repo, repo in linked_repos:
                # Find canonical checkout
                checkout_res = await db.execute(
                    select(RepositoryCheckout).where(
                        RepositoryCheckout.repository_id == repo.id,
                        RepositoryCheckout.canonical.is_(True),
                    )
                )
                checkout = checkout_res.scalar_one_or_none()
                repo_details.append(
                    {
                        "repository_id": str(repo.id),
                        "name": repo.name,
                        "role": ws_repo.role,
                        "primary": bool(ws_repo.primary),
                        "canonical_remote": repo.canonical_remote_identity,
                        "default_branch": repo.default_branch,
                        "checkout": {
                            "id": str(checkout.id) if checkout else None,
                            "path": checkout.path if checkout else None,
                            "branch": checkout.branch if checkout else None,
                            "revision": checkout.last_seen_revision if checkout else None,
                            "available": checkout.available if checkout else False,
                        }
                        if checkout
                        else None,
                    }
                )

            # Query associated active Workbench sessions
            from cptr.models.control import WorkbenchSession
            from sqlalchemy import or_

            workbench_res = await db.execute(
                select(WorkbenchSession)
                .where(
                    WorkbenchSession.user_id == user_id,
                    or_(
                        WorkbenchSession.workspace_id == workspace_id,
                        WorkbenchSession.active_workspace_id == workspace_id,
                    ),
                    WorkbenchSession.status == "OPEN",
                )
                .order_by(WorkbenchSession.updated_at.desc())
            )
            active_wb_sessions = workbench_res.scalars().all()
            workbench_info = {
                "active_sessions_count": len(active_wb_sessions),
                "sessions": [
                    {
                        "session_id": str(s.id),
                        "name": s.name,
                        "workspace_id": s.workspace_id,
                        "active_workspace_id": s.active_workspace_id,
                        "active_target_type": s.active_target_type,
                        "active_target_id": s.active_target_id,
                        "event_count": int(s.event_count or 0),
                        "updated_at": int(s.updated_at or 0),
                    }
                    for s in active_wb_sessions
                ],
            }

        # Context cache state
        cache_entry = self.cache.get_entry(workspace_id)
        cache_info = {
            "is_cached": cache_entry is not None and not cache_entry.is_expired(),
            "fingerprint": cache_entry.fingerprint if cache_entry else None,
            "cached_at_ms": cache_entry.cached_at_ms if cache_entry else None,
            "expires_at_ms": cache_entry.expires_at_ms if cache_entry else None,
            "access_count": cache_entry.access_count if cache_entry else 0,
            "tokens": cache_entry.tokens.to_dict() if cache_entry else {},
        }

        # Recent live events
        recent = list(self._recent_events.get(workspace_id, []))

        # Observability metrics snapshot
        metrics_snapshot = self.metrics.snapshot()

        status = "active"
        if not workspace.path:
            status = "unbound"
        elif any(r.get("checkout") and not r["checkout"].get("available") for r in repo_details):
            status = "degraded"

        health_checks = {
            "path_configured": bool(workspace.path),
            "path_exists": bool(workspace.path and os.path.exists(workspace.path)),
            "repositories_total": len(repo_details),
            "repositories_healthy": sum(
                1 for r in repo_details if r.get("checkout") and r["checkout"].get("available")
            ),
            "has_primary_repo": any(r.get("primary") for r in repo_details),
            "context_cached": cache_info["is_cached"],
            "workbench_sessions_active": workbench_info["active_sessions_count"],
        }
        health_summary = {
            "status": status,
            "checks": health_checks,
        }

        return {
            "version": 1,
            "workspace_id": str(workspace.id),
            "user_id": str(workspace.user_id),
            "name": workspace.name,
            "slug": workspace.slug,
            "path": workspace.path,
            "workspace_type": workspace.workspace_type,
            "status": status,
            "health": health_summary,
            "repositories": repo_details,
            "workbench": workbench_info,
            "context_cache": cache_info,
            "recent_events": recent,
            "metrics": metrics_snapshot,
        }


# Module Singletons
workspace_metrics = WorkspaceObservabilityMetrics()
workspace_context_cache = WorkspaceContextCache(metrics=workspace_metrics)
workspace_projection_service = WorkspaceLiveProjectionService(
    cache=workspace_context_cache,
    metrics=workspace_metrics,
)
