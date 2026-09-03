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

DEFAULT_RETRIEVAL_WEIGHTS: dict[str, float] = {
    "lexical": 0.13,
    "bm25": 0.17,
    "fuzzy": 0.06,
    "vector": 0.20,
    "graph": 0.08,
    "confidence": 0.08,
    "importance": 0.08,
    "temporal": 0.06,
    "trust": 0.07,
    "usage": 0.04,
    "procedure_success": 0.06,
    "failure_recurrence": 0.05,
}


def normalize_retrieval_weights(value: Mapping[str, float] | None = None) -> dict[str, float]:
    source = value or DEFAULT_RETRIEVAL_WEIGHTS
    weights = {
        key: max(0.0, min(1.0, float(source.get(key, default))))
        for key, default in DEFAULT_RETRIEVAL_WEIGHTS.items()
    }
    total = sum(weights.values())
    if total <= 0:
        return dict(DEFAULT_RETRIEVAL_WEIGHTS)
    return {key: weight / total for key, weight in weights.items()}


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
    bm25_score: float = 0.0,
    graph_score: float = 0.0,
    weights: Mapping[str, float] | None = None,
    intelligence: Mapping[str, float] | None = None,
) -> tuple[float, str, bool, dict[str, float]]:
    intelligence = intelligence or {}
    features = {
        "lexical": lexical_score(query, str(row.get("canonical_text") or "")),
        "bm25": max(0.0, min(1.0, float(bm25_score))),
        "fuzzy": local_similarity_score(query, str(row.get("canonical_text") or "")),
        "vector": max(0.0, min(1.0, float(vector_score))),
        "graph": max(0.0, min(1.0, float(graph_score))),
        "confidence": max(0.0, min(1.0, int(row.get("confidence_ppm") or 0) / 1_000_000)),
        "importance": max(0.0, min(1.0, int(row.get("importance_ppm") or 0) / 1_000_000)),
        "temporal": temporal_score(int(row.get("updated_at_ms") or 0), now_ms),
        "trust": _TRUST_WEIGHT.get(str(row.get("trust_level") or ""), 0.6),
        "usage": min(1.0, math.log1p(max(0, int(row.get("access_count") or 0))) / math.log(16)),
        "procedure_success": max(0.0, min(1.0, float(intelligence.get("procedure_success", 0.5)))),
        "failure_recurrence": max(
            0.0, min(1.0, float(intelligence.get("failure_recurrence", 0.0)))
        ),
    }
    normalized_weights = normalize_retrieval_weights(weights)
    score = sum(features[key] * normalized_weights[key] for key in normalized_weights)
    expires = row.get("verification_expires_at_ms")
    verification_stale = expires is not None and int(expires) < now_ms
    stale_factor = 0.58 if verification_stale else 1.0
    status = str(row.get("status") or "active")
    historical_factor = 1.0 if status == "active" else 0.32 if status == "disputed" else 0.45
    score *= stale_factor * historical_factor
    reason_bits = [
        f"bm25={features['bm25']:.2f}",
        f"vector={features['vector']:.2f}",
        f"graph={features['graph']:.2f}",
        f"lexical={features['lexical']:.2f}",
        f"trust={features['trust']:.2f}",
    ]
    if features["procedure_success"] != 0.5:
        reason_bits.append(f"procedure-success={features['procedure_success']:.2f}")
    if features["failure_recurrence"] > 0:
        reason_bits.append(f"recurrence={features['failure_recurrence']:.2f}")
    if verification_stale:
        reason_bits.append("verification-stale")
    if status != "active":
        reason_bits.append(status)
    return (
        round(max(0.0, min(1.0, score)), 6),
        ", ".join(reason_bits),
        verification_stale,
        features,
    )
