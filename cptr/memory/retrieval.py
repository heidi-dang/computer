"""Hybrid ranking policy for canonical CPTR memories."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Protocol

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._:/#-]*", re.IGNORECASE)
_TRUST_WEIGHT = {
    "user_directive": 1.0,
    "verified_system_fact": 0.98,
    "tool_result": 0.95,
    "managed_memory": 0.92,
    "agent_observation": 0.82,
    "agent_inference": 0.65,
    "third_party_content": 0.40,
    "untrusted_web_content": 0.15,
}


class VectorSearchPort(Protocol):
    async def score(
        self, *, user_id: str, workspace: str, query: str, memory_ids: list[str]
    ) -> Mapping[str, float]: ...


class NullVectorSearch:
    """No-op semantic adapter. A pgvector adapter can replace this without caller changes."""

    async def score(
        self, *, user_id: str, workspace: str, query: str, memory_ids: list[str]
    ) -> Mapping[str, float]:
        return {}


def tokens(value: str) -> set[str]:
    return {match.group(0).lower() for match in _TOKEN_RE.finditer(value or "")}


def _char_ngrams(value: str, size: int = 3) -> set[str]:
    normalized = " ".join((value or "").lower().split())
    if len(normalized) <= size:
        return {normalized} if normalized else set()
    return {normalized[index : index + size] for index in range(len(normalized) - size + 1)}


def lexical_score(query: str, text: str) -> float:
    query_tokens = tokens(query)
    if not query_tokens:
        return 0.0
    text_tokens = tokens(text)
    if not text_tokens:
        return 0.0
    coverage = len(query_tokens & text_tokens) / len(query_tokens)
    precision = len(query_tokens & text_tokens) / len(text_tokens)
    phrase = 1.0 if " ".join(query.lower().split()) in " ".join(text.lower().split()) else 0.0
    return min(1.0, coverage * 0.72 + precision * 0.13 + phrase * 0.15)


def local_similarity_score(query: str, text: str) -> float:
    """Cheap rebuildable fuzzy-vector signal used when no embedding adapter is configured."""
    left = _char_ngrams(query)
    right = _char_ngrams(text)
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    return intersection / math.sqrt(len(left) * len(right))


def temporal_score(updated_at_ms: int, now_ms: int, *, half_life_days: float = 45.0) -> float:
    age_ms = max(0, now_ms - int(updated_at_ms or 0))
    half_life_ms = max(1.0, half_life_days * 86_400_000.0)
    # Never erase old knowledge; stale memories remain retrievable with a bounded floor.
    return max(0.2, 0.5 ** (age_ms / half_life_ms))


def score_candidate(
    row: dict,
    *,
    query: str,
    now_ms: int,
    vector_score: float = 0.0,
) -> tuple[float, str, bool]:
    lexical = lexical_score(query, str(row.get("canonical_text") or ""))
    fuzzy = local_similarity_score(query, str(row.get("canonical_text") or ""))
    confidence = max(0.0, min(1.0, int(row.get("confidence_ppm") or 0) / 1_000_000))
    importance = max(0.0, min(1.0, int(row.get("importance_ppm") or 0) / 1_000_000))
    temporal = temporal_score(int(row.get("updated_at_ms") or 0), now_ms)
    trust = _TRUST_WEIGHT.get(str(row.get("trust_level") or ""), 0.6)
    usage = min(1.0, math.log1p(max(0, int(row.get("access_count") or 0))) / math.log(16))
    expires = row.get("verification_expires_at_ms")
    verification_stale = expires is not None and int(expires) < now_ms
    stale_factor = 0.58 if verification_stale else 1.0
    historical_factor = 0.45 if row.get("status") != "active" else 1.0

    score = (
        lexical * 0.34
        + fuzzy * 0.14
        + max(0.0, min(1.0, vector_score)) * 0.18
        + confidence * 0.10
        + importance * 0.09
        + temporal * 0.07
        + trust * 0.05
        + usage * 0.03
    )
    score *= stale_factor * historical_factor
    reason_bits = [f"lexical={lexical:.2f}", f"similarity={max(fuzzy, vector_score):.2f}"]
    if verification_stale:
        reason_bits.append("verification-stale")
    if row.get("status") != "active":
        reason_bits.append("historical")
    return round(max(0.0, min(1.0, score)), 6), ", ".join(reason_bits), verification_stale
