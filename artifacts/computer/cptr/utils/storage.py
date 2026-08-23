"""Pluggable blob storage.

Default backend stores files under DATA_DIR/uploads/.
Swap to S3/GCS by implementing StorageBackend and changing get_storage().
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

from cptr.env import DATA_DIR

UPLOADS_DIR = DATA_DIR / "uploads"


class StorageBackend(ABC):
    @abstractmethod
    async def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> bytes | None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...


class LocalStorage(StorageBackend):
    """Stores blobs as flat files in DATA_DIR/uploads/."""

    def __init__(self, base_dir: Path = UPLOADS_DIR):
        self._dir = base_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are UUIDs, no path traversal risk
        return self._dir / key

    async def put(self, key: str, data: bytes) -> None:
        self._path(key).write_bytes(data)

    async def get(self, key: str) -> bytes | None:
        p = self._path(key)
        return p.read_bytes() if p.exists() else None

    async def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()


_storage: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """Return the configured storage backend (singleton)."""
    global _storage
    if _storage is None:
        _storage = LocalStorage()
    return _storage
