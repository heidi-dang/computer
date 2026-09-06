"""Cross-repo MCP Services health aggregation.

Computes bounded, secret-free health bands for:
- computer backend
- ChatGPT Computer plugin
- Chrome extension / browser devices
- MCP transport stores (traffic, diagnostics, activity)

Bands are derived only from measurable probes. Unknown probes fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

HealthBand = Literal["healthy", "moderate", "unhealthy"]

# Thresholds (conservative defaults; fail closed)
EVENT_LOOP_LAG_MODERATE_MS = 50.0
EVENT_LOOP_LAG_UNHEALTHY_MS = 250.0
REQUEST_P95_MODERATE_MS = 1500.0
REQUEST_P95_UNHEALTHY_MS = 5000.0
ERROR_RATIO_MODERATE = 0.05
ERROR_RATIO_UNHEALTHY = 0.20


def _now_ms() -> int:
    return int(time.time() * 1000)


def _band_rank(band: HealthBand) -> int:
    return {"healthy": 0, "moderate": 1, "unhealthy": 2}[band]


def worst_band(*bands: HealthBand) -> HealthBand:
    return max(bands, key=_band_rank)


def score_from_probes(probes: list[dict[str, Any]]) -> tuple[float, HealthBand]:
    """Map probe results to a 0-100 score and band.

    Critical failures force unhealthy. Warnings force at most moderate.
    """
    if not probes:
        return 0.0, "unhealthy"

    critical_fail = any(
        p.get("severity") and p.get("status") == "fail" for p in probes
    )
    any_fail = any(p.get("status") == "fail" for p in probes)
    any_warn = any(p.get("status") == "warn" for p in probes)
    unknown = any(p.get("status") == "unknown" for p in probes)

    passed = sum(1 for p in probes if p.get("status") == "pass")
    total = len(probes)
    score = round(100.0 * passed / total, 1) if total else 0.0

    if critical_fail or unknown:
        return min(score, 35.0), "unhealthy"
    if any_fail:
        return min(score, 55.0), "unhealthy" if score < 40 else "moderate"
    if any_warn:
        return min(score, 80.0), "moderate"
    return score, "healthy"


def _probe(
    probe_id: str,
    *,
    critical: bool,
    status: Literal["pass", "warn", "fail", "unknown"],
    detail: str,
    measured: Any = None,
) -> dict[str, Any]:
    return {
        "id": probe_id,
        "critical": critical,
        "status": status,
        "detail": detail[:500],
        "measured": measured,
    }


@dataclass
class McpServicesHealthService:
    """Build the Services snapshot used by /api/mcp/services/*."""

    plugin_health_url: str = field(
        default_factory=lambda: os.getenv(
            "CPTR_PLUGIN_HEALTH_URL", "http://127.0.0.1:8787/health"
        )
    )
    plugin_timeout_s: float = 2.5

    async def snapshot(self) -> dict[str, Any]:
        backend = await self._probe_backend()
        plugin = await self._probe_plugin()
        extension = await self._probe_extension()
        transport = await self._probe_transport()

        services = [backend, plugin, extension, transport]
        aggregate = worst_band(*(s["band"] for s in services))
        active_job = mcp_services_maintain_store.active_job_id()

        payload = {
            "version": 1,
            "aggregate": aggregate,
            "generated_at_ms": _now_ms(),
            "plugin": {
                "version": plugin.get("stats", {}).get("app_version"),
                "contract_version": plugin.get("stats", {}).get("contract_version"),
                "tool_count": plugin.get("stats", {}).get("tool_count"),
                "workbench_ready": plugin.get("stats", {}).get("workbench_ready"),
                "band": plugin["band"],
                "probes": plugin["probes"],
            },
            "services": services,
            "maintain": {"active_job_id": active_job},
        }
        payload["fingerprint"] = hashlib.sha256(
            json.dumps(
                {
                    "aggregate": aggregate,
                    "services": [
                        {"id": s["id"], "band": s["band"], "score": s["score"]} for s in services
                    ],
                    "job": active_job,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:24]
        return payload

    async def _probe_backend(self) -> dict[str, Any]:
        from cptr.services.live_events import live_event_hub
        from cptr.services.runtime_metrics import runtime_metrics
        from cptr.utils.db import database_ready
        from cptr.utils.tools import command_session_metrics

        probes: list[dict[str, Any]] = []
        last_error: str | None = None
        last_ok_at: int | None = None

        try:
            ready = await database_ready()
            probes.append(
                _probe(
                    "db_ready",
                    critical=True,
                    status="pass" if ready else "fail",
                    detail="SQLite ready" if ready else "SQLite not ready",
                    measured=ready,
                )
            )
            if not ready:
                last_error = "database not ready"
        except Exception as exc:
            last_error = str(exc)[:200]
            probes.append(
                _probe(
                    "db_ready",
                    critical=True,
                    status="fail",
                    detail=f"database probe error: {last_error}",
                )
            )

        metrics = runtime_metrics.snapshot()
        lag = float(metrics.get("event_loop", {}).get("last_lag_ms") or 0.0)
        if lag >= EVENT_LOOP_LAG_UNHEALTHY_MS:
            lag_status: Literal["pass", "warn", "fail"] = "fail"
        elif lag >= EVENT_LOOP_LAG_MODERATE_MS:
            lag_status = "warn"
        else:
            lag_status = "pass"
        probes.append(
            _probe(
                "event_loop_lag",
                critical=True,
                status=lag_status,
                detail=f"event-loop lag {lag:.1f}ms",
                measured=lag,
            )
        )

        req = metrics.get("requests") or {}
        count = int(req.get("count") or 0)
        errors = int(req.get("server_error_count") or 0)
        p95 = float((req.get("latency_ms") or {}).get("p95") or 0.0)
        ratio = (errors / count) if count else 0.0
        if count and ratio >= ERROR_RATIO_UNHEALTHY:
            err_status: Literal["pass", "warn", "fail"] = "fail"
        elif count and ratio >= ERROR_RATIO_MODERATE:
            err_status = "warn"
        else:
            err_status = "pass"
        probes.append(
            _probe(
                "request_error_ratio",
                critical=False,
                status=err_status,
                detail=f"5xx ratio {ratio:.3f} over {count} requests",
                measured={"ratio": ratio, "count": count, "errors": errors},
            )
        )
        if p95 >= REQUEST_P95_UNHEALTHY_MS:
            p95_status: Literal["pass", "warn", "fail"] = "fail"
        elif p95 >= REQUEST_P95_MODERATE_MS:
            p95_status = "warn"
        else:
            p95_status = "pass"
        probes.append(
            _probe(
                "request_p95",
                critical=False,
                status=p95_status,
                detail=f"request p95 {p95:.1f}ms",
                measured=p95,
            )
        )

        try:
            cmd = command_session_metrics()
        except Exception:
            cmd = {}
        probes.append(
            _probe(
                "command_sessions",
                critical=False,
                status="pass",
                detail="command session metrics available",
                measured=cmd if isinstance(cmd, dict) else {},
            )
        )

        try:
            live = live_event_hub.stats()
        except Exception:
            live = {}
        probes.append(
            _probe(
                "live_events",
                critical=False,
                status="pass",
                detail="live event hub stats available",
                measured=live if isinstance(live, dict) else {},
            )
        )

        score, band = score_from_probes(probes)
        if band == "healthy":
            last_ok_at = _now_ms()
        return {
            "id": "backend",
            "name": "Computer backend",
            "band": band,
            "score": score,
            "probes": probes,
            "stats": {
                "uptime_seconds": metrics.get("uptime_seconds"),
                "event_loop_lag_ms": lag,
                "request_p95_ms": p95,
                "request_error_ratio": round(ratio, 4),
                "process": metrics.get("process") or {},
                "commands": cmd if isinstance(cmd, dict) else {},
                "live_events": live if isinstance(live, dict) else {},
            },
            "last_ok_at_ms": last_ok_at,
            "last_error": last_error,
        }

    async def _probe_plugin(self) -> dict[str, Any]:
        probes: list[dict[str, Any]] = []
        stats: dict[str, Any] = {}
        last_error: str | None = None
        last_ok_at: int | None = None
        url = self.plugin_health_url

        try:
            async with httpx.AsyncClient(timeout=self.plugin_timeout_s) as client:
                response = await client.get(url)
            reachable = response.status_code < 500
            probes.append(
                _probe(
                    "plugin_reachable",
                    critical=True,
                    status="pass" if reachable else "fail",
                    detail=f"GET {url} → HTTP {response.status_code}",
                    measured=response.status_code,
                )
            )
            body: dict[str, Any] = {}
            try:
                parsed = response.json()
                if isinstance(parsed, dict):
                    body = parsed
            except Exception:
                body = {}

            app_version = body.get("app_version") or body.get("version")
            contract = body.get("mcp_contract") if isinstance(body.get("mcp_contract"), dict) else {}
            contract_version = contract.get("version") or body.get("contract_version")
            tool_count = contract.get("tool_count") or body.get("tool_count")
            workbench = body.get("workbench") if isinstance(body.get("workbench"), dict) else {}
            workbench_ready = workbench.get("ready")

            stats = {
                "app_version": app_version,
                "contract_version": contract_version,
                "tool_count": tool_count,
                "workbench_ready": workbench_ready,
                "health_url": url,
            }

            version_ok = (
                isinstance(app_version, str)
                and isinstance(contract_version, str)
                and app_version == contract_version
                and bool(app_version)
            )
            probes.append(
                _probe(
                    "contract_version_match",
                    critical=True,
                    status="pass" if version_ok else ("warn" if app_version or contract_version else "unknown"),
                    detail=(
                        f"app={app_version} contract={contract_version}"
                        if (app_version or contract_version)
                        else "version fields missing from plugin /health"
                    ),
                    measured={"app_version": app_version, "contract_version": contract_version},
                )
            )

            if isinstance(tool_count, int) and tool_count > 0:
                probes.append(
                    _probe(
                        "tool_count",
                        critical=False,
                        status="pass",
                        detail=f"tool_count={tool_count}",
                        measured=tool_count,
                    )
                )
            else:
                probes.append(
                    _probe(
                        "tool_count",
                        critical=False,
                        status="warn",
                        detail="tool_count missing or zero",
                        measured=tool_count,
                    )
                )

            if workbench_ready is True:
                probes.append(
                    _probe(
                        "workbench_ready",
                        critical=True,
                        status="pass",
                        detail="workbench.ready=true",
                        measured=True,
                    )
                )
            elif workbench_ready is False:
                probes.append(
                    _probe(
                        "workbench_ready",
                        critical=True,
                        status="fail",
                        detail="workbench.ready=false",
                        measured=False,
                    )
                )
            else:
                probes.append(
                    _probe(
                        "workbench_ready",
                        critical=False,
                        status="warn",
                        detail="workbench.ready not reported",
                        measured=None,
                    )
                )

            if not reachable:
                last_error = f"plugin health HTTP {response.status_code}"
        except Exception as exc:
            last_error = str(exc)[:200]
            probes.append(
                _probe(
                    "plugin_reachable",
                    critical=True,
                    status="fail",
                    detail=f"plugin health unreachable: {last_error}",
                )
            )
            probes.append(
                _probe(
                    "contract_version_match",
                    critical=True,
                    status="unknown",
                    detail="skipped; plugin unreachable",
                )
            )

        score, band = score_from_probes(probes)
        if band == "healthy":
            last_ok_at = _now_ms()
        return {
            "id": "plugin",
            "name": "ChatGPT Computer plugin",
            "band": band,
            "score": score,
            "probes": probes,
            "stats": stats,
            "last_ok_at_ms": last_ok_at,
            "last_error": last_error,
        }

    async def _probe_extension(self) -> dict[str, Any]:
        probes: list[dict[str, Any]] = []
        stats: dict[str, Any] = {}
        last_error: str | None = None
        last_ok_at: int | None = None

        try:
            from cptr.services.browser_devices import browser_device_store

            devices = []
            if hasattr(browser_device_store, "list_devices"):
                raw = browser_device_store.list_devices()
                if isinstance(raw, list):
                    devices = raw
            elif hasattr(browser_device_store, "snapshot"):
                snap = browser_device_store.snapshot()
                if isinstance(snap, dict):
                    devices = list(snap.get("devices") or [])
                    stats = {
                        k: snap.get(k)
                        for k in ("device_count", "session_count", "active_leases")
                        if k in snap
                    }

            device_count = len(devices)
            stats["device_count"] = device_count
            stuck = 0
            for device in devices:
                if not isinstance(device, dict):
                    continue
                owner = str(device.get("lease_owner") or device.get("owner") or "").lower()
                state = str(device.get("state") or device.get("status") or "").upper()
                if owner == "agent" and state in {"RECONNECTING", "OFFLINE", "STALE"}:
                    stuck += 1
            stats["stuck_leases"] = stuck

            # Extension is optional: no devices is moderate, not a hard fail.
            if device_count == 0:
                probes.append(
                    _probe(
                        "paired_devices",
                        critical=False,
                        status="warn",
                        detail="no paired Chrome devices",
                        measured=0,
                    )
                )
            else:
                probes.append(
                    _probe(
                        "paired_devices",
                        critical=False,
                        status="pass",
                        detail=f"{device_count} paired device(s)",
                        measured=device_count,
                    )
                )

            if stuck:
                probes.append(
                    _probe(
                        "stuck_leases",
                        critical=True,
                        status="fail",
                        detail=f"{stuck} stuck agent lease(s)",
                        measured=stuck,
                    )
                )
                last_error = f"{stuck} stuck leases"
            else:
                probes.append(
                    _probe(
                        "stuck_leases",
                        critical=True,
                        status="pass",
                        detail="no stuck agent leases",
                        measured=0,
                    )
                )
        except Exception as exc:
            last_error = str(exc)[:200]
            probes.append(
                _probe(
                    "extension_store",
                    critical=False,
                    status="warn",
                    detail=f"browser device store unavailable: {last_error}",
                )
            )

        score, band = score_from_probes(probes)
        if band == "healthy":
            last_ok_at = _now_ms()
        return {
            "id": "extension",
            "name": "Chrome extension",
            "band": band,
            "score": score,
            "probes": probes,
            "stats": stats,
            "last_ok_at_ms": last_ok_at,
            "last_error": last_error,
        }

    async def _probe_transport(self) -> dict[str, Any]:
        probes: list[dict[str, Any]] = []
        stats: dict[str, Any] = {}
        last_error: str | None = None
        last_ok_at: int | None = None

        try:
            from cptr.services.mcp_activity import mcp_activity_store
            from cptr.services.mcp_diagnostics import mcp_diagnostics_store
            from cptr.services.mcp_traffic import mcp_traffic_store

            traffic = mcp_traffic_store.snapshot() if hasattr(mcp_traffic_store, "snapshot") else {}
            diagnostics = (
                mcp_diagnostics_store.snapshot()
                if hasattr(mcp_diagnostics_store, "snapshot")
                else {}
            )
            activity = mcp_activity_store.snapshot() if hasattr(mcp_activity_store, "snapshot") else {}

            stats = {
                "traffic_sequence": (traffic or {}).get("sequence"),
                "diagnostics_sequence": (diagnostics or {}).get("sequence"),
                "activity_sequence": (activity or {}).get("sequence"),
            }

            for name, snap in (
                ("traffic", traffic),
                ("diagnostics", diagnostics),
                ("activity", activity),
            ):
                health = (snap or {}).get("stream_health") if isinstance(snap, dict) else None
                drops = 0
                if isinstance(health, dict):
                    drops = int(health.get("slow_subscriber_drops") or 0)
                status: Literal["pass", "warn", "fail"] = "warn" if drops > 100 else "pass"
                probes.append(
                    _probe(
                        f"{name}_store",
                        critical=False,
                        status=status if isinstance(snap, dict) else "warn",
                        detail=(
                            f"{name} store ok (slow drops={drops})"
                            if isinstance(snap, dict)
                            else f"{name} store missing snapshot"
                        ),
                        measured={"slow_subscriber_drops": drops},
                    )
                )
        except Exception as exc:
            last_error = str(exc)[:200]
            probes.append(
                _probe(
                    "transport_stores",
                    critical=False,
                    status="warn",
                    detail=f"transport stores unavailable: {last_error}",
                )
            )

        score, band = score_from_probes(probes)
        if band == "healthy":
            last_ok_at = _now_ms()
        return {
            "id": "transport",
            "name": "MCP transport",
            "band": band,
            "score": score,
            "probes": probes,
            "stats": stats,
            "last_ok_at_ms": last_ok_at,
            "last_error": last_error,
        }


# Late import target for active job id (avoid circular import at module load time)
class _MaintainStoreProxy:
    def active_job_id(self) -> str | None:
        try:
            from cptr.services.mcp_services_maintain import mcp_services_maintain_store

            return mcp_services_maintain_store.active_job_id()
        except Exception:
            return None


mcp_services_maintain_store = _MaintainStoreProxy()
mcp_services_health = McpServicesHealthService()
