"""Persistent, bounded display aliases for MCP topology nodes."""

from __future__ import annotations

import re
from typing import Any

from cptr.models import Config

CONFIG_KEY = "mcp.topology.aliases"
MAX_ALIAS_LENGTH = 80
NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
CANONICAL_TOPOLOGY_LABELS = {
    "mcp-connector": "MCP Connector",
    "cptr-mcp": "CPTR MCP",
    "cptr-backend": "CPTR Backend",
}


def sanitize_topology_node_id(value: str) -> str:
    """Validate and return one canonical topology node ID."""
    if not isinstance(value, str) or not NODE_ID_RE.fullmatch(value):
        raise ValueError("invalid topology node id")
    return value


def sanitize_topology_alias(value: str | None) -> str | None:
    """Normalize one optional display alias without silently truncating it."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("topology alias must be a string or null")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("topology alias contains control characters")
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > MAX_ALIAS_LENGTH:
        raise ValueError(f"topology alias exceeds {MAX_ALIAS_LENGTH} characters")
    return normalized


def _sanitize_stored_aliases(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    aliases: dict[str, str] = {}
    for raw_node_id, raw_alias in value.items():
        if not isinstance(raw_node_id, str):
            continue
        try:
            node_id = sanitize_topology_node_id(raw_node_id)
            alias = sanitize_topology_alias(raw_alias if isinstance(raw_alias, str) else None)
        except ValueError:
            continue
        if alias is not None:
            aliases[node_id] = alias
    return aliases


def _config_response(aliases: dict[str, str]) -> dict[str, object]:
    return {
        "version": 1,
        "canonical_labels": dict(CANONICAL_TOPOLOGY_LABELS),
        "aliases": dict(aliases),
    }


async def get_topology_config() -> dict[str, object]:
    """Return canonical topology labels and persisted aliases."""
    aliases = _sanitize_stored_aliases(await Config.get(CONFIG_KEY))
    return _config_response(aliases)


async def update_topology_aliases(updates: dict[str, str | None]) -> dict[str, object]:
    """Partially merge/reset display aliases and persist the resulting mapping."""
    if not isinstance(updates, dict):
        raise ValueError("aliases must be an object")

    aliases = _sanitize_stored_aliases(await Config.get(CONFIG_KEY))
    for raw_node_id, raw_alias in updates.items():
        node_id = sanitize_topology_node_id(raw_node_id)
        alias = sanitize_topology_alias(raw_alias)
        if alias is None:
            aliases.pop(node_id, None)
        else:
            aliases[node_id] = alias

    await Config.upsert({CONFIG_KEY: aliases})
    return _config_response(aliases)
