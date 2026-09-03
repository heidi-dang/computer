"""Rebuildable embedding providers and the default SQL vector index.

The canonical Memory Core never depends on an external embedding service. If a
configured OpenAI-compatible endpoint is available, this module uses it; otherwise a
bounded deterministic hashing vector keeps vector retrieval operational offline.
Secrets are consumed at request time and are never persisted in memory tables.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

import httpx
from sqlalchemy import delete, or_, select

from cptr.models import MemoryEmbedding
from cptr.utils.db import get_session_factory


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0:
        return [0.0 for _ in values]
    return [value / norm for value in values]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


class EmbeddingProvider(Protocol):
    model_id: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbeddingProvider:
    """Deterministic local vector fallback with no model/network dependency."""

    model_id = "cptr-hashing-v1"

    def __init__(self, *, dimensions: int = 256) -> None:
        self.dimensions = max(32, min(int(dimensions), 4096))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        normalized = " ".join(str(text or "").lower().split())
        words = [word for word in normalized.replace("/", " ").split() if word]
        features = [*words]
        features.extend(f"{words[index]}::{words[index + 1]}" for index in range(len(words) - 1))
        vector = [0.0] * self.dimensions
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = -1.0 if digest[4] & 1 else 1.0
            vector[index] += sign
        return _normalize(vector)


class OpenAICompatibleEmbeddingProvider:
    """HTTP embedding provider compatible with the common `/v1/embeddings` schema."""

    def __init__(
        self,
        *,
        url: str,
        model_id: str,
        api_key: str = "",
        dimensions: int | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not str(url).strip():
            raise ValueError("embedding url is required")
        if not str(model_id).strip():
            raise ValueError("embedding model is required")
        self.url = str(url).strip()
        self.model_id = str(model_id).strip()
        self.api_key = str(api_key or "")
        self.dimensions = max(0, int(dimensions or 0))
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 120.0))

    def __repr__(self) -> str:
        return (
            f"OpenAICompatibleEmbeddingProvider(model_id={self.model_id!r}, "
            f"dimensions={self.dimensions or 'auto'})"
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {"model": self.model_id, "input": [str(item) for item in texts]}
        if self.dimensions > 0:
            payload["dimensions"] = self.dimensions
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(self.url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise RuntimeError("embedding endpoint returned an invalid row count")
        ordered = sorted(rows, key=lambda item: int(item.get("index", 0)))
        vectors: list[list[float]] = []
        for row in ordered:
            raw = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(raw, list) or not raw:
                raise RuntimeError("embedding endpoint returned an invalid vector")
            vector = [float(value) for value in raw]
            if any(not math.isfinite(value) for value in vector):
                raise RuntimeError("embedding endpoint returned a non-finite vector")
            if self.dimensions and len(vector) != self.dimensions:
                raise RuntimeError("embedding endpoint returned an unexpected dimension")
            if not self.dimensions:
                self.dimensions = len(vector)
            vectors.append(_normalize(vector))
        return vectors


def embedding_provider_from_env() -> EmbeddingProvider:
    url = os.getenv("CPTR_MEMORY_EMBEDDING_URL", "").strip()
    model = os.getenv("CPTR_MEMORY_EMBEDDING_MODEL", "").strip()
    if url and model:
        dimensions = int(os.getenv("CPTR_MEMORY_EMBEDDING_DIMENSIONS", "0") or 0)
        return OpenAICompatibleEmbeddingProvider(
            url=url,
            model_id=model,
            api_key=os.getenv("CPTR_MEMORY_EMBEDDING_API_KEY", ""),
            dimensions=dimensions or None,
            timeout_seconds=float(os.getenv("CPTR_MEMORY_EMBEDDING_TIMEOUT_SECONDS", "30") or 30),
        )
    return HashingEmbeddingProvider(
        dimensions=int(os.getenv("CPTR_MEMORY_HASHING_DIMENSIONS", "256") or 256)
    )


class SqlVectorIndex:
    """Portable durable vector cache backed by the CPTR SQL database."""

    def __init__(self, *, session_factory=None, provider: EmbeddingProvider | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()
        self.provider = provider or embedding_provider_from_env()

    @property
    def model_id(self) -> str:
        return self.provider.model_id

    @asynccontextmanager
    async def _session(self):
        async with self._session_factory() as db:
            yield db

    async def index_memory(self, row: dict[str, Any]) -> None:
        memory_id = str(row.get("memory_id") or "")
        text = str(row.get("canonical_text") or "").strip()
        if not memory_id or not text:
            return
        vector = (await self.provider.embed([text]))[0]
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                existing = await db.get(MemoryEmbedding, memory_id)
                if existing is None:
                    existing = MemoryEmbedding(
                        memory_id=memory_id,
                        user_id=str(row.get("user_id") or ""),
                        workspace=str(row.get("workspace") or ""),
                        model_id=self.provider.model_id,
                        dimensions=len(vector),
                        vector=vector,
                        updated_at_ms=now,
                    )
                    db.add(existing)
                else:
                    existing.user_id = str(row.get("user_id") or "")
                    existing.workspace = str(row.get("workspace") or "")
                    existing.model_id = self.provider.model_id
                    existing.dimensions = len(vector)
                    existing.vector = vector
                    existing.updated_at_ms = now

    async def score(
        self, *, user_id: str, workspace: str, query: str, memory_ids: list[str]
    ) -> dict[str, float]:
        if not query.strip() or not memory_ids:
            return {}
        query_vector = (await self.provider.embed([query]))[0]
        predicates = [
            MemoryEmbedding.memory_id.in_(memory_ids),
            MemoryEmbedding.user_id == user_id,
            MemoryEmbedding.model_id == self.provider.model_id,
        ]
        if workspace:
            predicates.append(
                or_(MemoryEmbedding.workspace == "", MemoryEmbedding.workspace == workspace)
            )
        else:
            predicates.append(MemoryEmbedding.workspace == "")
        async with self._session() as db:
            rows = list((await db.scalars(select(MemoryEmbedding).where(*predicates))).all())
        return {
            row.memory_id: cosine_similarity(
                query_vector, row.vector if isinstance(row.vector, list) else []
            )
            for row in rows
        }

    async def remove_memory(self, memory_id: str, *, user_id: str, workspace: str) -> None:
        async with self._session() as db:
            async with db.begin():
                await db.execute(
                    delete(MemoryEmbedding).where(
                        MemoryEmbedding.memory_id == memory_id,
                        MemoryEmbedding.user_id == user_id,
                        MemoryEmbedding.workspace == str(workspace or ""),
                    )
                )

    async def clear_scope(self, *, user_id: str, workspace: str) -> None:
        async with self._session() as db:
            async with db.begin():
                await db.execute(
                    delete(MemoryEmbedding).where(
                        MemoryEmbedding.user_id == user_id,
                        MemoryEmbedding.workspace == str(workspace or ""),
                    )
                )

    async def coverage(self, *, user_id: str, workspace: str | None = None) -> int:
        predicates = [
            MemoryEmbedding.user_id == user_id,
            MemoryEmbedding.model_id == self.provider.model_id,
        ]
        if workspace is not None:
            predicates.append(MemoryEmbedding.workspace == str(workspace or ""))
        async with self._session() as db:
            rows = list(
                (await db.scalars(select(MemoryEmbedding.memory_id).where(*predicates))).all()
            )
        return len(rows)

    def sanitized_config(self) -> dict[str, Any]:
        return {
            "backend": "sql-json-vector-cache",
            "model_id": self.provider.model_id,
            "dimensions": int(getattr(self.provider, "dimensions", 0) or 0),
        }
