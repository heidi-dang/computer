"""Deterministic evaluation harness for CPTR Memory retrieval quality."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cptr.memory.domain import MemoryQuery
from cptr.memory.ports import MemoryService


@dataclass(frozen=True)
class MemoryEvaluationCase:
    query: str
    relevant_memory_ids: frozenset[str]
    forbidden_memory_ids: frozenset[str] = field(default_factory=frozenset)
    kinds: tuple[str, ...] = ()


class MemoryEvaluationHarness:
    """Evaluate recall without changing canonical memory or learned ranking state."""

    def __init__(self, service: MemoryService) -> None:
        self.service = service

    async def evaluate(
        self,
        *,
        user_id: str,
        workspace: str,
        cases: list[MemoryEvaluationCase],
        k: int = 5,
    ) -> dict[str, Any]:
        if not cases:
            raise ValueError("at least one memory evaluation case is required")
        safe_k = max(1, min(int(k), 100))
        precision_values: list[float] = []
        recall_values: list[float] = []
        reciprocal_ranks: list[float] = []
        forbidden_hits = 0
        stale_top_hits = 0
        rows: list[dict[str, Any]] = []

        for case in cases:
            results = await self.service.search(
                MemoryQuery(
                    user_id=user_id,
                    workspace=workspace,
                    query=case.query,
                    kinds=case.kinds,
                    limit=safe_k,
                )
            )
            returned = [result.memory_id for result in results[:safe_k]]
            relevant_hits = [
                memory_id for memory_id in returned if memory_id in case.relevant_memory_ids
            ]
            precision = len(relevant_hits) / safe_k
            recall = (
                len(set(relevant_hits)) / len(case.relevant_memory_ids)
                if case.relevant_memory_ids
                else 1.0
            )
            rank = next(
                (
                    index + 1
                    for index, result in enumerate(results[:safe_k])
                    if result.memory_id in case.relevant_memory_ids
                ),
                None,
            )
            reciprocal_rank = 1.0 / rank if rank else 0.0
            forbidden = sorted(set(returned) & case.forbidden_memory_ids)
            if forbidden:
                forbidden_hits += 1
            if results and results[0].verification_stale:
                stale_top_hits += 1
            precision_values.append(precision)
            recall_values.append(recall)
            reciprocal_ranks.append(reciprocal_rank)
            rows.append(
                {
                    "query": case.query,
                    "returned_memory_ids": returned,
                    "relevant_hits": relevant_hits,
                    "forbidden_hits": forbidden,
                    "precision_at_k": precision,
                    "recall_at_k": recall,
                    "reciprocal_rank": reciprocal_rank,
                }
            )

        count = len(cases)
        precision_at_k = sum(precision_values) / count
        recall_at_k = sum(recall_values) / count
        mrr = sum(reciprocal_ranks) / count
        forbidden_hit_rate = forbidden_hits / count
        stale_top_rate = stale_top_hits / count
        # The score is intentionally transparent and bounded; leakage is penalized most heavily.
        quality_score = max(
            0.0,
            min(
                1.0,
                0.25 * precision_at_k
                + 0.35 * recall_at_k
                + 0.30 * mrr
                + 0.10 * (1.0 - stale_top_rate)
                - 0.50 * forbidden_hit_rate,
            ),
        )
        return {
            "case_count": count,
            "k": safe_k,
            "precision_at_k": round(precision_at_k, 6),
            "recall_at_k": round(recall_at_k, 6),
            "mrr": round(mrr, 6),
            "forbidden_hit_rate": round(forbidden_hit_rate, 6),
            "stale_top_rate": round(stale_top_rate, 6),
            "quality_score": round(quality_score, 6),
            "cases": rows,
        }
