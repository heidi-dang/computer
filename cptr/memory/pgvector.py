"""Optional PostgreSQL/pgvector derived index for CPTR Memory Core.

Canonical memory remains in the embedded SQL store. This adapter is rebuildable and
is selected only when CPTR_MEMORY_PGVECTOR_URL is configured. Connection strings and
embedding credentials are never included in repr/health payloads.
"""

from __future__ import annotations

import os
import time
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import create_async_engine

from cptr.memory.embeddings import EmbeddingProvider, embedding_provider_from_env

_TABLE = "cptr_memory_vector_index"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(format(float(value), ".12g") for value in vector) + "]"


def _asyncpg_url(value: str) -> str:
    candidate = str(value).strip()
    if candidate.startswith("postgresql://"):
        return "postgresql+asyncpg://" + candidate[len("postgresql://") :]
    if candidate.startswith("postgres://"):
        return "postgresql+asyncpg://" + candidate[len("postgres://") :]
    return candidate


class PgVectorIndex:
    def __init__(self, *, url: str, provider: EmbeddingProvider | None = None) -> None:
        if not str(url).strip():
            raise ValueError("pgvector url is required")
        self._url = _asyncpg_url(url)
        self.provider = provider or embedding_provider_from_env()
        self._engine = None
        self._initialized = False
        self._indexed_dimensions: set[int] = set()

    @classmethod
    def from_env(cls, *, provider: EmbeddingProvider | None = None) -> "PgVectorIndex | None":
        url = os.getenv("CPTR_MEMORY_PGVECTOR_URL", "").strip()
        return cls(url=url, provider=provider) if url else None

    @property
    def model_id(self) -> str:
        return self.provider.model_id

    def __repr__(self) -> str:
        return (
            f"PgVectorIndex(model_id={self.provider.model_id!r}, "
            f"dimensions={int(getattr(self.provider, 'dimensions', 0) or 0) or 'auto'})"
        )

    def _get_engine(self):
        if self._engine is None:
            self._engine = create_async_engine(self._url, pool_pre_ping=True)
        return self._engine

    async def initialize(self) -> None:
        if self._initialized:
            return
        engine = self._get_engine()
        async with engine.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_TABLE} (
                        memory_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        workspace TEXT NOT NULL DEFAULT '',
                        model_id TEXT NOT NULL,
                        dimensions INTEGER NOT NULL,
                        embedding vector NOT NULL,
                        updated_at_ms BIGINT NOT NULL
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_owner_model "
                    f"ON {_TABLE} (user_id, workspace, model_id)"
                )
            )
        self._initialized = True
        dimensions = int(getattr(self.provider, "dimensions", 0) or 0)
        if dimensions:
            await self._ensure_dimension_index(dimensions)

    async def _ensure_dimension_index(self, dimensions: int) -> None:
        dimensions = max(1, int(dimensions))
        if dimensions in self._indexed_dimensions:
            return
        await self.initialize()
        index_name = f"ix_{_TABLE}_hnsw_cosine_{dimensions}"
        engine = self._get_engine()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {index_name} ON {_TABLE} "
                    f"USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops) "
                    f"WHERE dimensions = {dimensions}"
                )
            )
        self._indexed_dimensions.add(dimensions)

    async def index_memory(self, row: dict[str, Any]) -> None:
        memory_id = str(row.get("memory_id") or "")
        canonical_text = str(row.get("canonical_text") or "").strip()
        if not memory_id or not canonical_text:
            return
        vector = (await self.provider.embed([canonical_text]))[0]
        await self.initialize()
        await self._ensure_dimension_index(len(vector))
        engine = self._get_engine()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"""
                    INSERT INTO {_TABLE}
                        (memory_id, user_id, workspace, model_id, dimensions, embedding, updated_at_ms)
                    VALUES
                        (:memory_id, :user_id, :workspace, :model_id, :dimensions,
                         CAST(:embedding AS vector), :updated_at_ms)
                    ON CONFLICT (memory_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        workspace = EXCLUDED.workspace,
                        model_id = EXCLUDED.model_id,
                        dimensions = EXCLUDED.dimensions,
                        embedding = EXCLUDED.embedding,
                        updated_at_ms = EXCLUDED.updated_at_ms
                    """
                ),
                {
                    "memory_id": memory_id,
                    "user_id": str(row.get("user_id") or ""),
                    "workspace": str(row.get("workspace") or ""),
                    "model_id": self.provider.model_id,
                    "dimensions": len(vector),
                    "embedding": _vector_literal(vector),
                    "updated_at_ms": _now_ms(),
                },
            )

    async def score(
        self, *, user_id: str, workspace: str, query: str, memory_ids: list[str]
    ) -> dict[str, float]:
        ids = list(dict.fromkeys(item for item in memory_ids if item))
        if not query.strip() or not ids:
            return {}
        vector = (await self.provider.embed([query]))[0]
        await self.initialize()
        await self._ensure_dimension_index(len(vector))
        workspace_clause = (
            "AND workspace = ''"
            if not workspace
            else "AND (workspace = '' OR workspace = :workspace)"
        )
        statement = text(
            f"""
            SELECT memory_id,
                   GREATEST(0.0, LEAST(1.0,
                       1.0 - (embedding::vector(:dimensions) <=> CAST(:embedding AS vector)))) AS score
            FROM {_TABLE}
            WHERE user_id = :user_id
              AND model_id = :model_id
              AND dimensions = :dimensions
              {workspace_clause}
              AND memory_id IN :memory_ids
            """
        ).bindparams(bindparam("memory_ids", expanding=True))
        engine = self._get_engine()
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        statement,
                        {
                            "user_id": user_id,
                            "workspace": str(workspace or ""),
                            "model_id": self.provider.model_id,
                            "dimensions": len(vector),
                            "embedding": _vector_literal(vector),
                            "memory_ids": ids,
                        },
                    )
                )
                .mappings()
                .all()
            )
        return {str(row["memory_id"]): float(row["score"] or 0.0) for row in rows}

    async def remove_memory(self, memory_id: str, *, user_id: str, workspace: str) -> None:
        await self.initialize()
        engine = self._get_engine()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"DELETE FROM {_TABLE} "
                    "WHERE memory_id = :memory_id AND user_id = :user_id AND workspace = :workspace"
                ),
                {
                    "memory_id": memory_id,
                    "user_id": user_id,
                    "workspace": str(workspace or ""),
                },
            )

    async def clear_scope(self, *, user_id: str, workspace: str) -> None:
        await self.initialize()
        engine = self._get_engine()
        async with engine.begin() as connection:
            await connection.execute(
                text(f"DELETE FROM {_TABLE} WHERE user_id = :user_id AND workspace = :workspace"),
                {"user_id": user_id, "workspace": str(workspace or "")},
            )

    async def coverage(self, *, user_id: str, workspace: str | None = None) -> int:
        await self.initialize()
        clauses = ["user_id = :user_id", "model_id = :model_id"]
        params: dict[str, Any] = {"user_id": user_id, "model_id": self.provider.model_id}
        if workspace is not None:
            clauses.append("workspace = :workspace")
            params["workspace"] = str(workspace or "")
        engine = self._get_engine()
        async with engine.connect() as connection:
            value = (
                await connection.execute(
                    text(f"SELECT COUNT(*) FROM {_TABLE} WHERE " + " AND ".join(clauses)),
                    params,
                )
            ).scalar_one()
        return int(value or 0)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._initialized = False
            self._indexed_dimensions.clear()

    def sanitized_config(self) -> dict[str, Any]:
        return {
            "backend": "postgresql+pgvector",
            "model_id": self.provider.model_id,
            "dimensions": int(getattr(self.provider, "dimensions", 0) or 0),
            "hnsw": True,
        }
