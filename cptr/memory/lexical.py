"""Portable rebuildable BM25 index for canonical CPTR memory."""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import delete, or_, select

from cptr.models import MemoryLexicalDocument, MemoryLexicalTerm
from cptr.utils.db import get_session_factory

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._:/#-]*", re.IGNORECASE)


def _now_ms() -> int:
    return int(time.time() * 1000)


def lexical_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(value or ""):
        token = match.group(0).lower().rstrip(".,;:!?")
        if token:
            tokens.append(token)
    return tokens


class MemoryLexicalIndex:
    """BM25 index stored in ordinary SQL tables so SQLite/Postgres behave identically."""

    def __init__(self, *, session_factory=None, k1: float = 1.2, b: float = 0.75) -> None:
        self._session_factory = session_factory or get_session_factory()
        self.k1 = max(0.1, min(float(k1), 4.0))
        self.b = max(0.0, min(float(b), 1.0))

    @asynccontextmanager
    async def _session(self):
        async with self._session_factory() as db:
            yield db

    async def index_memory(self, row: dict[str, Any]) -> None:
        memory_id = str(row.get("memory_id") or "")
        if not memory_id:
            return
        tokens = lexical_tokens(str(row.get("canonical_text") or ""))
        counts = Counter(tokens)
        now = _now_ms()
        async with self._session() as db:
            async with db.begin():
                await db.execute(
                    delete(MemoryLexicalTerm).where(MemoryLexicalTerm.memory_id == memory_id)
                )
                document = await db.get(MemoryLexicalDocument, memory_id)
                if document is None:
                    document = MemoryLexicalDocument(
                        memory_id=memory_id,
                        user_id=str(row.get("user_id") or ""),
                        workspace=str(row.get("workspace") or ""),
                        token_count=len(tokens),
                        updated_at_ms=now,
                    )
                    db.add(document)
                else:
                    document.user_id = str(row.get("user_id") or "")
                    document.workspace = str(row.get("workspace") or "")
                    document.token_count = len(tokens)
                    document.updated_at_ms = now
                for term, frequency in counts.items():
                    db.add(
                        MemoryLexicalTerm(
                            memory_id=memory_id,
                            user_id=str(row.get("user_id") or ""),
                            workspace=str(row.get("workspace") or ""),
                            term=term,
                            term_frequency=int(frequency),
                            updated_at_ms=now,
                        )
                    )

    async def score(
        self, *, user_id: str, workspace: str, query: str, memory_ids: list[str]
    ) -> dict[str, float]:
        query_terms = list(dict.fromkeys(lexical_tokens(query)))
        candidates = list(dict.fromkeys(str(item) for item in memory_ids if str(item)))
        if not query_terms or not candidates:
            return {}
        document_predicates = [
            MemoryLexicalDocument.memory_id.in_(candidates),
            MemoryLexicalDocument.user_id == user_id,
        ]
        if workspace:
            document_predicates.append(
                or_(
                    MemoryLexicalDocument.workspace == "",
                    MemoryLexicalDocument.workspace == workspace,
                )
            )
        else:
            document_predicates.append(MemoryLexicalDocument.workspace == "")
        async with self._session() as db:
            documents = list(
                (await db.scalars(select(MemoryLexicalDocument).where(*document_predicates))).all()
            )
            if not documents:
                return {}
            doc_ids = [row.memory_id for row in documents]
            terms = list(
                (
                    await db.scalars(
                        select(MemoryLexicalTerm).where(
                            MemoryLexicalTerm.memory_id.in_(doc_ids),
                            MemoryLexicalTerm.term.in_(query_terms),
                        )
                    )
                ).all()
            )
        document_length = {row.memory_id: max(1, int(row.token_count or 0)) for row in documents}
        average_length = sum(document_length.values()) / max(1, len(document_length))
        by_term: dict[str, list[MemoryLexicalTerm]] = {}
        for row in terms:
            by_term.setdefault(row.term, []).append(row)
        raw_scores: dict[str, float] = {memory_id: 0.0 for memory_id in doc_ids}
        document_count = len(document_length)
        for term in query_terms:
            postings = by_term.get(term, [])
            document_frequency = len({row.memory_id for row in postings})
            if document_frequency <= 0:
                continue
            idf = math.log(
                1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            for posting in postings:
                frequency = max(0.0, float(posting.term_frequency or 0))
                length = float(document_length.get(posting.memory_id, 1))
                denominator = frequency + self.k1 * (
                    1.0 - self.b + self.b * (length / max(1.0, average_length))
                )
                if denominator <= 0:
                    continue
                raw_scores[posting.memory_id] += idf * (frequency * (self.k1 + 1.0) / denominator)
        maximum = max(raw_scores.values(), default=0.0)
        if maximum <= 0:
            return {}
        return {
            memory_id: max(0.0, min(1.0, score / maximum))
            for memory_id, score in raw_scores.items()
            if score > 0
        }

    async def remove_memory(self, memory_id: str, *, user_id: str, workspace: str) -> None:
        async with self._session() as db:
            async with db.begin():
                await db.execute(
                    delete(MemoryLexicalTerm).where(
                        MemoryLexicalTerm.memory_id == memory_id,
                        MemoryLexicalTerm.user_id == user_id,
                        MemoryLexicalTerm.workspace == str(workspace or ""),
                    )
                )
                await db.execute(
                    delete(MemoryLexicalDocument).where(
                        MemoryLexicalDocument.memory_id == memory_id,
                        MemoryLexicalDocument.user_id == user_id,
                        MemoryLexicalDocument.workspace == str(workspace or ""),
                    )
                )

    async def clear_scope(self, *, user_id: str, workspace: str) -> None:
        workspace = str(workspace or "")
        async with self._session() as db:
            async with db.begin():
                await db.execute(
                    delete(MemoryLexicalTerm).where(
                        MemoryLexicalTerm.user_id == user_id,
                        MemoryLexicalTerm.workspace == workspace,
                    )
                )
                await db.execute(
                    delete(MemoryLexicalDocument).where(
                        MemoryLexicalDocument.user_id == user_id,
                        MemoryLexicalDocument.workspace == workspace,
                    )
                )

    async def coverage(self, *, user_id: str, workspace: str | None = None) -> int:
        predicates = [MemoryLexicalDocument.user_id == user_id]
        if workspace is not None:
            predicates.append(MemoryLexicalDocument.workspace == str(workspace or ""))
        async with self._session() as db:
            rows = list(
                (await db.scalars(select(MemoryLexicalDocument.memory_id).where(*predicates))).all()
            )
        return len(rows)

    def sanitized_config(self) -> dict[str, Any]:
        return {"backend": "portable-sql-bm25", "k1": self.k1, "b": self.b}
