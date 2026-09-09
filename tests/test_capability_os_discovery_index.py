"""Tests for CapabilityOsDiscoveryIndex."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from cptr.services.capability_os.discovery_index import (
    CapabilityOsDiscoveryIndex,
    DiscoveryIndexSyncError,
    McpRegistryEntry,
)


def _entry(server_id: str, effects: list, reputation: float = 0.5) -> McpRegistryEntry:
    return McpRegistryEntry(
        server_id=server_id,
        name=server_id,
        description="",
        effects=effects,
        install_uri=f"npm:{server_id}",
        reputation_score=reputation,
    )


def _index(*entries) -> CapabilityOsDiscoveryIndex:
    idx = CapabilityOsDiscoveryIndex(registry_url="http://example.com/registry")
    for e in entries:
        idx.add_entry(e)
    return idx


def test_find_single_effect():
    a = _entry("a", ["pods.read", "logs.read"])
    b = _entry("b", ["pods.write"])
    c = _entry("c", ["pods.read"])
    idx = _index(a, b, c)
    result = idx.find(["pods.read"])
    ids = [e.server_id for e in result]
    assert "a" in ids
    assert "c" in ids
    assert "b" not in ids


def test_find_multiple_effects():
    a = _entry("a", ["pods.read", "logs.read"])
    b = _entry("b", ["pods.read"])
    idx = _index(a, b)
    result = idx.find(["pods.read", "logs.read"])
    ids = [e.server_id for e in result]
    assert ids == ["a"]


def test_find_forbidden():
    safe = _entry("safe", ["pods.read"])
    danger = _entry("danger", ["pods.read", "pods.delete"])
    idx = _index(safe, danger)
    result = idx.find(["pods.read"], forbidden_effects=["pods.delete"])
    ids = [e.server_id for e in result]
    assert ids == ["safe"]


def test_find_empty_required_returns_all():
    entries = [_entry(f"srv{i}", ["x.read"]) for i in range(5)]
    idx = _index(*entries)
    result = idx.find([])
    assert len(result) == 5


def test_find_limit():
    entries = [_entry(f"srv{i}", ["common.read"]) for i in range(20)]
    idx = _index(*entries)
    result = idx.find(["common.read"], limit=5)
    assert len(result) == 5


def test_reputation_sort():
    low = _entry("low", ["x.read"], reputation=0.3)
    high = _entry("high", ["x.read"], reputation=0.9)
    idx = _index(low, high)
    result = idx.find(["x.read"])
    assert result[0].server_id == "high"


def test_get_by_id_found():
    e = _entry("myserver", ["y.read"])
    idx = _index(e)
    assert idx.get_by_id("myserver") is e


def test_get_by_id_missing():
    idx = _index()
    assert idx.get_by_id("nonexistent") is None


@pytest.mark.asyncio
async def test_sync_parses():
    registry_response = {
        "servers": [
            {
                "id": "srv1",
                "name": "My Server",
                "description": "A test server",
                "effects": ["pods.read"],
                "install_uri": "npm:my-server",
                "install_type": "npm",
                "qualified": True,
                "reputation_score": 0.8,
            }
        ]
    }
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=registry_response)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    idx = CapabilityOsDiscoveryIndex(registry_url="http://example.com/registry")
    count = await idx.sync(mock_client)

    assert count == 1
    entry = idx.get_by_id("srv1")
    assert entry is not None
    assert "pods.read" in entry.effects


@pytest.mark.asyncio
async def test_sync_error():
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("network failure"))

    idx = CapabilityOsDiscoveryIndex(registry_url="http://example.com/registry")
    with pytest.raises(DiscoveryIndexSyncError):
        await idx.sync(mock_client)
