"""Durable MCP-visible usage accounting and observed engineering session metrics."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import case, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from cptr.models.metrics import McpEngineeringSession, McpUsageEvent
from cptr.services.mcp_diagnostics import McpUsageDiagnostic
from cptr.services.mcp_pricing import project_usage_cost
from cptr.utils.db import get_db

_PICO_USD = Decimal("1000000000000")

_MUTATION_TOOLS = {
    "cptr_code_write_file",
    "cptr_code_edit_file",
    "cptr_code_apply_edits",
    "cptr_code_create_directory",
    "cptr_code_move_file",
    "cptr_code_delete_file",
    "cptr_direct_workers_integrate",
}
_VERIFICATION_TOOLS = {
    "cptr_workspace_run_test_target",
    "cptr_code_get_git_status",
    "cptr_get_diff",
    "cptr_workspace_release_readiness",
    "cptr_lsp_request",
}
_READ_TOOLS = {
    "cptr_code_list_files",
    "cptr_code_read_file",
    "cptr_code_read_many_files",
    "cptr_code_search_files",
    "cptr_workspace_tree",
    "cptr_workspace_read_many",
    "cptr_workspace_search_symbols",
    "cptr_workspace_discover_tests",
    "cptr_workspace_dependency_summary",
    "cptr_workspace_package_scripts",
    "cptr_fdx_intelligence",
}


def _cost_pico(value: object) -> int:
    if value is None:
        return 0
    return int((Decimal(str(value)) * _PICO_USD).to_integral_value())


def _cost_text(pico: int) -> str:
    value = Decimal(int(pico)) / _PICO_USD
    text = format(value.quantize(Decimal("0.000000000001")), "f").rstrip("0").rstrip(".")
    return text or "0"


def _model_key(projected: dict[str, object]) -> str:
    canonical = projected.get("model_canonical")
    if isinstance(canonical, str) and canonical:
        return canonical
    reported = projected.get("model_reported")
    if isinstance(reported, str) and reported.strip():
        return "reported:" + "-".join(reported.strip().lower().split())[:120]
    return "unreported"


def _session_key(event: McpUsageDiagnostic) -> str:
    return event.session_id or f"unscoped:{event.client_id}"


def _category_counts(tool_name: str) -> tuple[int, int, int]:
    return (
        1 if tool_name in _MUTATION_TOOLS else 0,
        1 if tool_name in _VERIFICATION_TOOLS else 0,
        1 if tool_name in _READ_TOOLS else 0,
    )


def _operational_score(row: McpEngineeringSession) -> tuple[float, float, float]:
    calls = max(1, int(row.tool_calls or 0))
    reliability = int(row.successful_tool_calls or 0) / calls
    mutations = int(row.coding_mutations or 0)
    verifications = int(row.verification_calls or 0)
    if mutations > 0:
        verification_ratio = min(1.0, verifications / mutations)
    else:
        verification_ratio = 1.0 if verifications > 0 else 0.0
    score = round(reliability * 70.0 + verification_ratio * 30.0, 2)
    return reliability, verification_ratio, score


class McpUsageStore:
    def __init__(self, *, session_factory=None) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self):
        if self._session_factory is not None:
            async with self._session_factory() as db:
                yield db
            return
        async with await get_db() as db:
            yield db

    async def ingest(
        self, owner_id: str, events: Iterable[McpUsageDiagnostic | object]
    ) -> set[str]:
        candidate_by_id: dict[
            str, tuple[McpUsageDiagnostic, dict[str, object], dict[str, object]]
        ] = {}
        for event in events:
            if not isinstance(event, McpUsageDiagnostic) or event.event_id in candidate_by_id:
                continue
            projected = project_usage_cost(event)
            values = {
                "id": event.event_id,
                "user_id": owner_id,
                "timestamp_ms": event.timestamp_ms,
                "request_id": event.request_id,
                "correlation_id": event.correlation_id,
                "session_id": event.session_id,
                "client_id": event.client_id,
                "model_reported": projected.get("model_reported"),
                "model_canonical": projected.get("model_canonical"),
                "model_source": event.model_source,
                "tool_name": event.tool_name,
                "input_tokens_estimated": event.input_tokens_estimated,
                "output_tokens_estimated": event.output_tokens_estimated,
                "estimator_method": event.estimator_method,
                "estimator_exact_for_model": 1 if event.estimator_exact_for_model else 0,
                "status": event.status,
                "pricing_status": projected.get("pricing_status") or "unknown_model",
                "pricing_version": projected.get("pricing_version") or "unknown",
                "input_usd_per_million": projected.get("input_usd_per_million"),
                "cached_input_usd_per_million": projected.get("cached_input_usd_per_million"),
                "output_usd_per_million": projected.get("output_usd_per_million"),
                "input_cost_pico_usd": _cost_pico(projected.get("input_cost_usd")),
                "output_cost_pico_usd": _cost_pico(projected.get("output_cost_usd")),
                "simulated_cost_pico_usd": _cost_pico(projected.get("simulated_cost_usd")),
            }
            candidate_by_id[event.event_id] = (event, projected, values)
        candidates = list(candidate_by_id.values())
        if not candidates:
            return set()

        async with self._session() as db:
            result = await db.execute(
                sqlite_insert(McpUsageEvent)
                .values([values for _, _, values in candidates])
                .on_conflict_do_nothing(index_elements=["id"])
                .returning(McpUsageEvent.id)
            )
            accepted = {str(event_id) for event_id in result.scalars().all()}

            grouped: dict[tuple[str, str], dict[str, object]] = {}
            for event, projected, values in candidates:
                if event.event_id not in accepted:
                    continue
                key = (_session_key(event), _model_key(projected))
                mutation, verification, read = _category_counts(event.tool_name)
                group = grouped.get(key)
                if group is None:
                    group = {
                        "session_key": key[0],
                        "model_key": key[1],
                        "session_id": event.session_id,
                        "client_id": event.client_id,
                        "model_reported": projected.get("model_reported"),
                        "model_canonical": projected.get("model_canonical"),
                        "first_seen_ms": event.timestamp_ms,
                        "last_seen_ms": event.timestamp_ms,
                        "tool_calls": 0,
                        "successful_tool_calls": 0,
                        "failed_tool_calls": 0,
                        "coding_mutations": 0,
                        "verification_calls": 0,
                        "read_calls": 0,
                        "input_tokens_estimated": 0,
                        "output_tokens_estimated": 0,
                        "simulated_cost_pico_usd": 0,
                    }
                    grouped[key] = group
                group["first_seen_ms"] = min(int(group["first_seen_ms"]), event.timestamp_ms)
                group["last_seen_ms"] = max(int(group["last_seen_ms"]), event.timestamp_ms)
                group["tool_calls"] = int(group["tool_calls"]) + 1
                if event.status == "complete":
                    group["successful_tool_calls"] = int(group["successful_tool_calls"]) + 1
                else:
                    group["failed_tool_calls"] = int(group["failed_tool_calls"]) + 1
                group["coding_mutations"] = int(group["coding_mutations"]) + mutation
                group["verification_calls"] = int(group["verification_calls"]) + verification
                group["read_calls"] = int(group["read_calls"]) + read
                group["input_tokens_estimated"] = int(group["input_tokens_estimated"]) + int(
                    event.input_tokens_estimated
                )
                group["output_tokens_estimated"] = int(group["output_tokens_estimated"]) + int(
                    event.output_tokens_estimated
                )
                group["simulated_cost_pico_usd"] = int(group["simulated_cost_pico_usd"]) + int(
                    values["simulated_cost_pico_usd"]
                )

            for group in grouped.values():
                await self._accumulate_engineering_group(db, owner_id, group)
            await db.commit()
        return accepted

    async def _accumulate_engineering_group(
        self,
        db,
        owner_id: str,
        group: dict[str, object],
    ) -> None:
        row = await db.scalar(
            select(McpEngineeringSession).where(
                McpEngineeringSession.user_id == owner_id,
                McpEngineeringSession.session_key == group["session_key"],
                McpEngineeringSession.model_key == group["model_key"],
            )
        )
        if row is None:
            db.add(
                McpEngineeringSession(
                    user_id=owner_id,
                    session_key=group["session_key"],
                    session_id=group["session_id"],
                    client_id=group["client_id"],
                    model_key=group["model_key"],
                    model_reported=group["model_reported"],
                    model_canonical=group["model_canonical"],
                    first_seen_ms=group["first_seen_ms"],
                    last_seen_ms=group["last_seen_ms"],
                    tool_calls=group["tool_calls"],
                    successful_tool_calls=group["successful_tool_calls"],
                    failed_tool_calls=group["failed_tool_calls"],
                    coding_mutations=group["coding_mutations"],
                    verification_calls=group["verification_calls"],
                    read_calls=group["read_calls"],
                    input_tokens_estimated=group["input_tokens_estimated"],
                    output_tokens_estimated=group["output_tokens_estimated"],
                    simulated_cost_pico_usd=group["simulated_cost_pico_usd"],
                )
            )
            return
        row.first_seen_ms = min(int(row.first_seen_ms), int(group["first_seen_ms"]))
        row.last_seen_ms = max(int(row.last_seen_ms), int(group["last_seen_ms"]))
        row.tool_calls = int(row.tool_calls or 0) + int(group["tool_calls"])
        row.successful_tool_calls = int(row.successful_tool_calls or 0) + int(
            group["successful_tool_calls"]
        )
        row.failed_tool_calls = int(row.failed_tool_calls or 0) + int(group["failed_tool_calls"])
        row.coding_mutations = int(row.coding_mutations or 0) + int(group["coding_mutations"])
        row.verification_calls = int(row.verification_calls or 0) + int(group["verification_calls"])
        row.read_calls = int(row.read_calls or 0) + int(group["read_calls"])
        row.input_tokens_estimated = int(row.input_tokens_estimated or 0) + int(
            group["input_tokens_estimated"]
        )
        row.output_tokens_estimated = int(row.output_tokens_estimated or 0) + int(
            group["output_tokens_estimated"]
        )
        row.simulated_cost_pico_usd = int(row.simulated_cost_pico_usd or 0) + int(
            group["simulated_cost_pico_usd"]
        )

    async def summary(self, owner_id: str, *, now_ms: int | None = None) -> dict[str, object]:
        now = datetime.fromtimestamp(
            (
                now_ms
                if now_ms is not None
                else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            )
            / 1000,
            tz=timezone.utc,
        )
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        starts = {
            "week": int(week_start.timestamp() * 1000),
            "month": int(month_start.timestamp() * 1000),
            "rolling_7d": int((now - timedelta(days=7)).timestamp() * 1000),
            "rolling_30d": int((now - timedelta(days=30)).timestamp() * 1000),
            "all_time": None,
        }
        async with self._session() as db:
            periods = {
                name: await self._aggregate_period(db, owner_id, start_ms)
                for name, start_ms in starts.items()
            }
        return {
            **periods,
            "generated_at_ms": int(now.timestamp() * 1000),
            "timezone": "UTC",
            "week_starts_on": "monday",
        }

    async def _aggregate_period(self, db, owner_id: str, start_ms: int | None) -> dict[str, object]:
        predicate = [McpUsageEvent.user_id == owner_id]
        if start_ms is not None:
            predicate.append(McpUsageEvent.timestamp_ms >= start_ms)
        current_case = case((McpUsageEvent.pricing_status == "current", 1), else_=0)
        stale_case = case((McpUsageEvent.pricing_status == "stale", 1), else_=0)
        unpriced_case = case(
            (McpUsageEvent.pricing_status.notin_(["current", "stale"]), 1), else_=0
        )
        row = (
            await db.execute(
                select(
                    func.count(McpUsageEvent.id),
                    func.coalesce(func.sum(McpUsageEvent.input_tokens_estimated), 0),
                    func.coalesce(func.sum(McpUsageEvent.output_tokens_estimated), 0),
                    func.coalesce(func.sum(McpUsageEvent.simulated_cost_pico_usd), 0),
                    func.coalesce(func.sum(current_case), 0),
                    func.coalesce(func.sum(stale_case), 0),
                    func.coalesce(func.sum(unpriced_case), 0),
                ).where(*predicate)
            )
        ).one()
        requests = int(row[0] or 0)
        input_tokens = int(row[1] or 0)
        output_tokens = int(row[2] or 0)
        cost_pico = int(row[3] or 0)
        return {
            "requests": requests,
            "input_tokens_estimated": input_tokens,
            "output_tokens_estimated": output_tokens,
            "total_tokens_estimated": input_tokens + output_tokens,
            "simulated_cost_usd": _cost_text(cost_pico),
            "priced_events": int(row[4] or 0),
            "stale_events": int(row[5] or 0),
            "unpriced_events": int(row[6] or 0),
        }

    async def engineering_sessions(self, owner_id: str, *, limit: int = 50) -> dict[str, object]:
        safe_limit = max(1, min(int(limit), 200))
        async with self._session() as db:
            rows = list(
                (
                    await db.scalars(
                        select(McpEngineeringSession)
                        .where(McpEngineeringSession.user_id == owner_id)
                        .order_by(McpEngineeringSession.last_seen_ms.desc())
                        .limit(safe_limit)
                    )
                ).all()
            )
        sessions = []
        for row in rows:
            reliability, verification_ratio, score = _operational_score(row)
            input_tokens = int(row.input_tokens_estimated or 0)
            output_tokens = int(row.output_tokens_estimated or 0)
            sessions.append(
                {
                    "session_id": row.session_id,
                    "client_id": row.client_id,
                    "model_reported": row.model_reported,
                    "model_canonical": row.model_canonical,
                    "first_seen_ms": int(row.first_seen_ms),
                    "last_seen_ms": int(row.last_seen_ms),
                    "tool_calls": int(row.tool_calls or 0),
                    "successful_tool_calls": int(row.successful_tool_calls or 0),
                    "failed_tool_calls": int(row.failed_tool_calls or 0),
                    "coding_mutations": int(row.coding_mutations or 0),
                    "verification_calls": int(row.verification_calls or 0),
                    "read_calls": int(row.read_calls or 0),
                    "input_tokens_estimated": input_tokens,
                    "output_tokens_estimated": output_tokens,
                    "total_tokens_estimated": input_tokens + output_tokens,
                    "simulated_cost_usd": _cost_text(int(row.simulated_cost_pico_usd or 0)),
                    "reliability": round(reliability, 4),
                    "verification_ratio": round(verification_ratio, 4),
                    "operational_score": score,
                    "comparable": False,
                }
            )
        return {
            "comparable": False,
            "comparability": "observed_real_work_only",
            "score_formula": "70% successful tool-call reliability + 30% bounded verification-to-mutation ratio",
            "disclaimer": "Observed real-work sessions are not comparable benchmark runs because task difficulty differs.",
            "sessions": sessions,
        }


mcp_usage_store = McpUsageStore()
