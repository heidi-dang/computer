"""Semantic MCP Discovery Index for Capability OS.

Indexes MCP server entries by their declared effect tags.
Query: find(required_effects=['pods.read', 'pod.logs.read'])

Separate from the Dark Factory discovery pipeline.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class McpRegistryEntry:
    server_id: str
    name: str
    description: str
    effects: list
    install_uri: str
    install_type: str = "npm"
    qualified: bool = False
    reputation_score: float = 0.5
    last_seen: float = 0.0

    def __post_init__(self):
        if not self.last_seen:
            self.last_seen = time.time()


class DiscoveryIndexSyncError(Exception):
    """Raised when syncing from registry fails."""


class CapabilityOsDiscoveryIndex:
    """Searchable index of MCP capabilities by required effects."""

    def __init__(self, registry_url: str = "https://registry.smithery.ai/servers",
                 cache_ttl_seconds: int = 3600) -> None:
        self._registry_url = registry_url
        self._cache_ttl = cache_ttl_seconds
        self._entries: dict = {}
        self._effect_index: dict = {}
        self._last_sync: float = 0.0

    async def sync(self, http_client: Any) -> int:
        """Fetch from registry, index by effects. Returns count synced."""
        try:
            response = await http_client.get(self._registry_url, timeout=15)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise DiscoveryIndexSyncError(
                "Failed to sync from " + self._registry_url + ": " + str(exc)
            ) from exc
        entries = self._parse_registry_response(data)
        for entry in entries:
            self._entries[entry.server_id] = entry
            self._index_entry(entry)
        self._last_sync = time.time()
        return len(entries)

    def find(self, required_effects: list, forbidden_effects: list | None = None,
             limit: int = 10) -> list:
        """Find MCPs declaring ALL required_effects and NONE of forbidden_effects."""
        forbidden_effects = forbidden_effects or []
        if not required_effects:
            candidates = list(self._entries.values())
        else:
            matching_ids = None
            for effect in required_effects:
                ids = self._effect_index.get(effect, set())
                matching_ids = ids if matching_ids is None else matching_ids & ids
            candidates = [self._entries[sid] for sid in (matching_ids or set())
                          if sid in self._entries]
        if forbidden_effects:
            candidates = [e for e in candidates
                          if not any(f in e.effects for f in forbidden_effects)]
        candidates.sort(key=lambda e: (-e.reputation_score, e.name))
        return candidates[:limit]

    def get_by_id(self, server_id: str):
        return self._entries.get(server_id)

    def add_entry(self, entry: McpRegistryEntry) -> None:
        """Add or update an entry."""
        self._entries[entry.server_id] = entry
        self._index_entry(entry)

    def _index_entry(self, entry: McpRegistryEntry) -> None:
        for effect in entry.effects:
            self._effect_index.setdefault(effect, set()).add(entry.server_id)

    def _parse_registry_response(self, data: Any) -> list:
        entries = []
        servers = data.get("servers", []) if isinstance(data, dict) else []
        for s in servers:
            server_id = s.get("qualifiedName") or s.get("id") or ""
            if not server_id:
                continue
            effects = s.get("effects", []) or self._infer_effects(s)
            entries.append(McpRegistryEntry(
                server_id=server_id,
                name=s.get("displayName") or s.get("name") or server_id,
                description=s.get("description") or "",
                effects=effects,
                install_uri=s.get("installUri") or s.get("packageName") or server_id,
                install_type=s.get("installType", "npm"),
            ))
        return entries

    def _infer_effects(self, server_data: dict) -> list:
        effects = []
        for tool in server_data.get("tools", []):
            name = (tool.get("name") or "").lower()
            if any(w in name for w in ("read", "get", "list", "fetch")):
                effects.append(name + ".read")
            if any(w in name for w in ("write", "create", "update", "put")):
                effects.append(name + ".write")
            if any(w in name for w in ("delete", "remove", "drop")):
                effects.append(name + ".delete")
        return list(set(effects))[:20]
