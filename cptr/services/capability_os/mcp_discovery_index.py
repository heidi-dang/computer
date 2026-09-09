"""Persistent semantic MCP discovery index keyed by declared effects.

Registry names and descriptions are discovery hints only. Matching is performed
against explicit, normalized effect declarations; CPTR never infers authority or
effects from natural-language names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.services.factory_discovery import DiscoveryCandidate


class McpDiscoveryIndexError(ValueError):
    pass


def _effects(values: Iterable[object]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw in values:
        value = str(raw).strip().lower()
        if not value:
            continue
        if len(value) > 256 or any(ord(char) < 33 for char in value):
            raise McpDiscoveryIndexError("MCP effect identifiers must be bounded printable tokens")
        normalized.add(value)
    if len(normalized) > 256:
        raise McpDiscoveryIndexError("MCP effect set exceeds bound")
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class McpDiscoveryMatch:
    entry_id: str
    provider: str
    server_id: str
    version: str | None
    origin_uri: str
    source_uri: str | None
    effects: tuple[str, ...]
    permissions: tuple[str, ...]
    metadata: dict[str, Any]
    refreshed_at_ms: int

    def to_api(self) -> dict[str, Any]:
        return {
            "entryId": self.entry_id,
            "provider": self.provider,
            "serverId": self.server_id,
            "version": self.version,
            "originUri": self.origin_uri,
            "sourceUri": self.source_uri,
            "effects": list(self.effects),
            "permissions": list(self.permissions),
            "metadata": dict(self.metadata),
            "refreshedAtMs": self.refreshed_at_ms,
        }


class McpSemanticDiscoveryIndex:
    def __init__(self, *, store: SqlCapabilityOsStore, clock_ms) -> None:
        self._store = store
        self._clock_ms = clock_ms

    @staticmethod
    def candidate_effects(candidate: DiscoveryCandidate) -> tuple[str, ...]:
        metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
        explicit = metadata.get("effects")
        if explicit is None:
            return ()
        if not isinstance(explicit, (list, tuple)):
            raise McpDiscoveryIndexError("MCP registry effects metadata must be an array")
        return _effects(explicit)

    async def sync_candidate(self, candidate: DiscoveryCandidate) -> bool:
        effects = self.candidate_effects(candidate)
        if not effects:
            # Unknown effects are deliberately not guessed from names, descriptions,
            # permissions, or package metadata.
            return False
        await self._store.upsert_mcp_discovery_entry(
            entry_id=candidate.identity,
            provider=candidate.provider,
            server_id=candidate.name,
            version=candidate.version,
            origin_uri=candidate.origin_uri,
            source_uri=candidate.source_uri,
            effects=list(effects),
            permissions=list(candidate.permissions),
            metadata=dict(candidate.metadata),
            refreshed_at_ms=int(self._clock_ms()),
        )
        return True

    async def sync_candidates(self, candidates: Iterable[DiscoveryCandidate]) -> int:
        count = 0
        for candidate in candidates:
            if await self.sync_candidate(candidate):
                count += 1
        return count

    async def sync_qualified_adapter(self, row) -> bool:
        spec = dict(row.spec or {})
        declared = spec.get("effects")
        if not isinstance(declared, list) or not declared:
            return False
        effects = _effects(declared)
        if not effects:
            return False
        permissions: list[str] = []
        for item in spec.get("permissions") or ():
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip()
            resource = str(item.get("resource") or "").strip()
            if action and resource:
                permissions.append(f"{action}:{resource}")
        server_id = str(spec.get("serverId") or row.artifact_id).strip()
        remote = spec.get("remote") if isinstance(spec.get("remote"), dict) else {}
        package = spec.get("package") if isinstance(spec.get("package"), dict) else {}
        origin_uri = str(
            remote.get("url") or package.get("source") or "cptr://qualified-mcp"
        ).strip()
        await self._store.upsert_mcp_discovery_entry(
            entry_id=f"artifact:{row.content_digest}",
            provider="qualified-mcp",
            server_id=server_id,
            version=str(row.version),
            origin_uri=origin_uri,
            source_uri=None,
            effects=list(effects),
            permissions=permissions,
            metadata={"artifactDigest": row.content_digest, "state": row.state},
            refreshed_at_ms=int(self._clock_ms()),
        )
        return True

    async def query(
        self,
        *,
        required_effects: Iterable[object],
        forbidden_effects: Iterable[object] = (),
        limit: int = 20,
    ) -> tuple[McpDiscoveryMatch, ...]:
        required = _effects(required_effects)
        forbidden = _effects(forbidden_effects)
        if not required:
            raise McpDiscoveryIndexError(
                "semantic MCP discovery requires at least one required effect"
            )
        if set(required) & set(forbidden):
            raise McpDiscoveryIndexError("required and forbidden MCP effects overlap")
        rows = await self._store.query_mcp_discovery_entries(
            required_effects=required,
            forbidden_effects=forbidden,
            limit=limit,
        )
        return tuple(
            McpDiscoveryMatch(
                entry_id=row.entry_id,
                provider=row.provider,
                server_id=row.server_id,
                version=row.version,
                origin_uri=row.origin_uri,
                source_uri=row.source_uri,
                effects=tuple(str(item) for item in row.effects or ()),
                permissions=tuple(str(item) for item in row.permissions or ()),
                metadata=dict(row.metadata_json or {}),
                refreshed_at_ms=int(row.refreshed_at_ms),
            )
            for row in rows
        )
