"""MCP Services health aggregation: probe runners + band scoring.

Bands are derived only from measurable probes. Unknown/unreachable fails closed
(never invents healthy). Thresholds match Phase 0 lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Literal, cast

import httpx

Band = Literal["healthy", "moderate", "unhealthy"]
SERVICE_IDS = ("backend", "plugin", "extension", "mcp_transport")

# Cross-repo contract expectations. Defaults match the current paired plugin
# release, while environment overrides avoid hard-coding future releases into
# the health implementation.
EXPECTED_CONTRACT_VERSION = (
    os.environ.get("CPTR_MCP_EXPECTED_CONTRACT_VERSION", "1.4.5").strip() or "1.4.5"
)
try:
    EXPECTED_TOOL_COUNT = max(
        1, int(os.environ.get("CPTR_MCP_EXPECTED_TOOL_COUNT", "83"))
    )
except ValueError:
    EXPECTED_TOOL_COUNT = 83

# Thresholds
EVENT_LOOP_LAG_HEALTHY_MS = 50.0
EVENT_LOOP_LAG_UNHEALTHY_MS = 250.0
REQUEST_P95_HEALTHY_MS = 200.0
REQUEST_P95_UNHEALTHY_MS = 1000.0
REQUEST_SAMPLE_MIN = 20
OPEN_FDS_HEALTHY = 2000
OPEN_FDS_UNHEALTHY = 4000


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ms_now() -> int:
    return int(time.time() * 1000)


def _plugin_update_url_from_env() -> str | None:
    explicit = os.environ.get("CPTR_MCP_PLUGIN_UPDATE_URL", "").strip()
    if explicit:
        return explicit
    base = os.environ.get("CPTR_MCP_PLUGIN_BASE_URL", "").strip()
    if base:
        return f"{base.rstrip('/')}/plugin/update"
    return None


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


@dataclass
class ProbeResult:
    id: str
    ok: bool
    critical: bool
    band_hint: Band
    detail: str
    measured_at: str = field(default_factory=_iso_now)
    value: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ok": self.ok,
            "critical": self.critical,
            "band_hint": self.band_hint,
            "detail": self.detail,
            "measured_at": self.measured_at,
            "value": self.value,
        }


def band_from_probes(probes: list[ProbeResult]) -> Band:
    """Aggregate probe list into a service band. Fail closed."""
    if not probes:
        return "unhealthy"
    if any(p.critical and p.band_hint == "unhealthy" for p in probes):
        return "unhealthy"
    if any(p.band_hint == "unhealthy" for p in probes):
        return "unhealthy"
    if any(p.band_hint == "moderate" for p in probes):
        return "moderate"
    return "healthy"


def aggregate_bands(bands: list[Band]) -> Band:
    if not bands:
        return "unhealthy"
    if any(b == "unhealthy" for b in bands):
        return "unhealthy"
    if any(b == "moderate" for b in bands):
        return "moderate"
    return "healthy"


def score_from_band(band: Band) -> float:
    if band == "healthy":
        return 1.0
    if band == "moderate":
        return 0.5
    return 0.0


@dataclass
class PluginIdentity:
    version: str | None = None
    contract_version: str | None = None
    tool_count: int | None = None
    release_sha: str | None = None
    refresh_required: bool | None = None
    source: str = "unknown"
    updated_at_ms: int | None = None


class PluginIdentityCache:
    """Last-known plugin identity (filled by telemetry or tests). No secrets."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._identity = PluginIdentity()

    def set(self, identity: PluginIdentity) -> None:
        with self._lock:
            identity.updated_at_ms = identity.updated_at_ms or _ms_now()
            self._identity = identity

    def get(self) -> PluginIdentity:
        with self._lock:
            return PluginIdentity(
                version=self._identity.version,
                contract_version=self._identity.contract_version,
                tool_count=self._identity.tool_count,
                release_sha=self._identity.release_sha,
                refresh_required=self._identity.refresh_required,
                source=self._identity.source,
                updated_at_ms=self._identity.updated_at_ms,
            )


plugin_identity_cache = PluginIdentityCache()


def probe_backend_readiness(*, database_ready: bool | None, error: str | None = None) -> ProbeResult:
    if database_ready is None:
        return ProbeResult(
            id="backend.readiness",
            ok=False,
            critical=True,
            band_hint="unhealthy",
            detail=error or "readiness unknown",
            value=None,
        )
    if database_ready:
        return ProbeResult(
            id="backend.readiness",
            ok=True,
            critical=True,
            band_hint="healthy",
            detail="database ready",
            value="ready",
        )
    return ProbeResult(
        id="backend.readiness",
        ok=False,
        critical=True,
        band_hint="unhealthy",
        detail=error or "database not ready",
        value="not_ready",
    )


def probe_backend_liveness(*, process_up: bool = True) -> ProbeResult:
    if process_up:
        return ProbeResult(
            id="backend.liveness",
            ok=True,
            critical=True,
            band_hint="healthy",
            detail="process responding",
            value="ok",
        )
    return ProbeResult(
        id="backend.liveness",
        ok=False,
        critical=True,
        band_hint="unhealthy",
        detail="process not responding",
        value="down",
    )


def probe_event_loop_lag(lag_ms: float | None) -> ProbeResult:
    if lag_ms is None:
        return ProbeResult(
            id="backend.event_loop_lag",
            ok=False,
            critical=False,
            band_hint="moderate",
            detail="event loop lag unknown",
            value=None,
        )
    if lag_ms < EVENT_LOOP_LAG_HEALTHY_MS:
        band: Band = "healthy"
    elif lag_ms <= EVENT_LOOP_LAG_UNHEALTHY_MS:
        band = "moderate"
    else:
        band = "unhealthy"
    return ProbeResult(
        id="backend.event_loop_lag",
        ok=band != "unhealthy",
        critical=False,
        band_hint=band,
        detail=f"last_lag_ms={lag_ms}",
        value=lag_ms,
    )


def probe_request_p95(p95_ms: float | None, sample_count: int) -> ProbeResult:
    if sample_count < REQUEST_SAMPLE_MIN or p95_ms is None:
        return ProbeResult(
            id="backend.request_p95",
            ok=False,
            critical=False,
            band_hint="moderate",
            detail=f"insufficient samples ({sample_count})",
            value={"p95_ms": p95_ms, "samples": sample_count},
        )
    if p95_ms < REQUEST_P95_HEALTHY_MS:
        band: Band = "healthy"
    elif p95_ms <= REQUEST_P95_UNHEALTHY_MS:
        band = "moderate"
    else:
        band = "unhealthy"
    return ProbeResult(
        id="backend.request_p95",
        ok=band != "unhealthy",
        critical=False,
        band_hint=band,
        detail=f"p95_ms={p95_ms} samples={sample_count}",
        value={"p95_ms": p95_ms, "samples": sample_count},
    )


def probe_open_fds(open_fds: int | None) -> ProbeResult:
    if open_fds is None:
        return ProbeResult(
            id="backend.open_fds",
            ok=False,
            critical=False,
            band_hint="moderate",
            detail="open_fds unavailable on this platform",
            value=None,
        )
    if open_fds < OPEN_FDS_HEALTHY:
        band: Band = "healthy"
    elif open_fds <= OPEN_FDS_UNHEALTHY:
        band = "moderate"
    else:
        band = "unhealthy"
    return ProbeResult(
        id="backend.open_fds",
        ok=band != "unhealthy",
        critical=False,
        band_hint=band,
        detail=f"open_fds={open_fds}",
        value=open_fds,
    )


def probe_worker_watchdog(snapshot: dict[str, Any] | None) -> ProbeResult:
    if not snapshot:
        return ProbeResult(
            id="backend.worker_watchdog",
            ok=False,
            critical=False,
            band_hint="moderate",
            detail="worker watchdog unavailable",
            value=None,
        )
    raw_band = str(snapshot.get("aggregate") or "moderate")
    band = (
        cast(Band, raw_band)
        if raw_band in {"healthy", "moderate", "unhealthy"}
        else "moderate"
    )
    workers = snapshot.get("workers") if isinstance(snapshot.get("workers"), dict) else {}
    unhealthy = sorted(
        name
        for name, item in workers.items()
        if isinstance(item, dict)
        and str(item.get("status")) in {"missing", "crashed", "restart_suspended"}
    )
    stalled = sorted(
        name
        for name, item in workers.items()
        if isinstance(item, dict) and str(item.get("status")) == "stalled"
    )
    degraded = sorted(
        name
        for name, item in workers.items()
        if isinstance(item, dict) and str(item.get("status")) == "degraded"
    )
    return ProbeResult(
        id="backend.worker_watchdog",
        ok=band == "healthy",
        critical=True,
        band_hint=band,
        detail=(
            f"workers={len(workers)} unhealthy={len(unhealthy)} "
            f"stalled={len(stalled)} degraded={len(degraded)}"
        ),
        value={
            "worker_count": len(workers),
            "unhealthy": unhealthy,
            "stalled": stalled,
            "degraded": degraded,
            "workers": workers,
        },
    )


def probe_plugin_identity(
    identity: PluginIdentity,
    *,
    expected_contract_version: str = EXPECTED_CONTRACT_VERSION,
    expected_tool_count: int = EXPECTED_TOOL_COUNT,
) -> list[ProbeResult]:
    probes: list[ProbeResult] = []
    has_any = any(
        [
            identity.version,
            identity.contract_version,
            identity.tool_count is not None,
        ]
    )
    if not has_any:
        probes.append(
            ProbeResult(
                id="plugin.identity",
                ok=False,
                critical=True,
                band_hint="unhealthy",
                detail="plugin identity unreachable / not reported",
                value=None,
            )
        )
        return probes

    probes.append(
        ProbeResult(
            id="plugin.identity",
            ok=True,
            critical=True,
            band_hint="healthy",
            detail=f"source={identity.source}",
            value={
                "version": identity.version,
                "contract_version": identity.contract_version,
                "tool_count": identity.tool_count,
                "release_sha": identity.release_sha,
            },
        )
    )

    if identity.contract_version is None:
        probes.append(
            ProbeResult(
                id="plugin.contract_version",
                ok=False,
                critical=True,
                band_hint="unhealthy",
                detail="contract_version missing",
                value=None,
            )
        )
    elif identity.contract_version != expected_contract_version:
        probes.append(
            ProbeResult(
                id="plugin.contract_version",
                ok=False,
                critical=True,
                band_hint="unhealthy",
                detail=(
                    f"contract drift: got {identity.contract_version}, "
                    f"expected {expected_contract_version}"
                ),
                value=identity.contract_version,
            )
        )
    else:
        probes.append(
            ProbeResult(
                id="plugin.contract_version",
                ok=True,
                critical=True,
                band_hint="healthy",
                detail=f"contract_version={identity.contract_version}",
                value=identity.contract_version,
            )
        )

    if identity.tool_count is None:
        probes.append(
            ProbeResult(
                id="plugin.tool_count",
                ok=False,
                critical=False,
                band_hint="moderate",
                detail="tool_count missing",
                value=None,
            )
        )
    elif int(identity.tool_count) != int(expected_tool_count):
        probes.append(
            ProbeResult(
                id="plugin.tool_count",
                ok=False,
                critical=False,
                band_hint="moderate",
                detail=(
                    f"tool_count drift: got {identity.tool_count}, "
                    f"expected {expected_tool_count}"
                ),
                value=identity.tool_count,
            )
        )
    else:
        probes.append(
            ProbeResult(
                id="plugin.tool_count",
                ok=True,
                critical=False,
                band_hint="healthy",
                detail=f"tool_count={identity.tool_count}",
                value=identity.tool_count,
            )
        )

    if identity.refresh_required is True:
        probes.append(
            ProbeResult(
                id="plugin.refresh_required",
                ok=True,
                critical=False,
                band_hint="healthy",
                detail="ChatGPT frozen tool snapshot refresh required (operator action)",
                value=True,
            )
        )
    elif identity.refresh_required is False:
        probes.append(
            ProbeResult(
                id="plugin.refresh_required",
                ok=True,
                critical=False,
                band_hint="healthy",
                detail="refresh not required",
                value=False,
            )
        )
    return probes


def probe_extension_devices(
    devices: list[dict[str, Any]],
) -> list[ProbeResult]:
    """Score extension from device list (status, connected)."""
    active = [d for d in devices if str(d.get("status", "")).upper() == "ACTIVE"]
    connected_active = [d for d in active if bool(d.get("connected"))]

    if not active:
        return [
            ProbeResult(
                id="extension.paired_device",
                ok=False,
                critical=True,
                band_hint="unhealthy",
                detail="no ACTIVE paired device",
                value={"active": 0, "connected": 0, "total": len(devices)},
            )
        ]

    if not connected_active:
        return [
            ProbeResult(
                id="extension.paired_device",
                ok=False,
                critical=True,
                band_hint="moderate",
                detail="ACTIVE device present but not connected",
                value={
                    "active": len(active),
                    "connected": 0,
                    "total": len(devices),
                },
            )
        ]

    return [
        ProbeResult(
            id="extension.paired_device",
            ok=True,
            critical=True,
            band_hint="healthy",
            detail=f"{len(connected_active)} connected ACTIVE device(s)",
            value={
                "active": len(active),
                "connected": len(connected_active),
                "total": len(devices),
            },
        )
    ]


def probe_extension_leases(leases: list[dict[str, Any]]) -> ProbeResult:
    """Stuck agent leases are moderate, not critical."""
    stuck = [
        lease
        for lease in leases
        if str(lease.get("owner", "")) == "agent"
        and (
            lease.get("expires_at") is not None
            and int(lease["expires_at"]) < _ms_now()
            or lease.get("orphaned") is True
        )
    ]
    if stuck:
        return ProbeResult(
            id="extension.leases",
            ok=False,
            critical=False,
            band_hint="moderate",
            detail=f"{len(stuck)} stuck/expired agent lease(s)",
            value={"stuck": len(stuck), "total": len(leases)},
        )
    return ProbeResult(
        id="extension.leases",
        ok=True,
        critical=False,
        band_hint="healthy",
        detail="no stuck agent leases",
        value={"stuck": 0, "total": len(leases)},
    )


def probe_mcp_transport(
    *,
    diagnostics_ok: bool,
    traffic_ok: bool,
    diagnostics_error: str | None = None,
    traffic_error: str | None = None,
    slow_subscriber_drops: int = 0,
) -> list[ProbeResult]:
    probes: list[ProbeResult] = []
    if not diagnostics_ok:
        probes.append(
            ProbeResult(
                id="mcp.diagnostics_store",
                ok=False,
                critical=False,
                band_hint="unhealthy",
                detail=diagnostics_error or "diagnostics store unresponsive",
            )
        )
    else:
        probes.append(
            ProbeResult(
                id="mcp.diagnostics_store",
                ok=True,
                critical=False,
                band_hint="healthy",
                detail="diagnostics store responsive",
            )
        )
    if not traffic_ok:
        probes.append(
            ProbeResult(
                id="mcp.traffic_store",
                ok=False,
                critical=False,
                band_hint="unhealthy",
                detail=traffic_error or "traffic store unresponsive",
            )
        )
    else:
        probes.append(
            ProbeResult(
                id="mcp.traffic_store",
                ok=True,
                critical=False,
                band_hint="healthy",
                detail="traffic store responsive",
            )
        )
    if slow_subscriber_drops > 50:
        probes.append(
            ProbeResult(
                id="mcp.sse_pressure",
                ok=False,
                critical=False,
                band_hint="moderate",
                detail=f"slow_subscriber_drops={slow_subscriber_drops}",
                value=slow_subscriber_drops,
            )
        )
    else:
        probes.append(
            ProbeResult(
                id="mcp.sse_pressure",
                ok=True,
                critical=False,
                band_hint="healthy",
                detail=f"slow_subscriber_drops={slow_subscriber_drops}",
                value=slow_subscriber_drops,
            )
        )
    return probes


class McpServicesHealthService:
    """Build bounded services snapshot from live probes."""

    def __init__(
        self,
        *,
        identity_cache: PluginIdentityCache | None = None,
        expected_contract_version: str = EXPECTED_CONTRACT_VERSION,
        expected_tool_count: int = EXPECTED_TOOL_COUNT,
        plugin_update_url: str | None = None,
        plugin_probe_timeout_seconds: float = 2.0,
        plugin_probe_interval_seconds: float | None = None,
    ) -> None:
        self.identity_cache = identity_cache or plugin_identity_cache
        self.expected_contract_version = expected_contract_version
        self.expected_tool_count = expected_tool_count
        self.plugin_update_url = (
            plugin_update_url
            if plugin_update_url is not None
            else _plugin_update_url_from_env()
        )
        self.plugin_probe_timeout_seconds = max(
            0.1, min(float(plugin_probe_timeout_seconds), 10.0)
        )
        if plugin_probe_interval_seconds is None:
            try:
                plugin_probe_interval_seconds = float(
                    os.environ.get("CPTR_MCP_PLUGIN_PROBE_INTERVAL_SECONDS", "15")
                )
            except ValueError:
                plugin_probe_interval_seconds = 15.0
        self.plugin_probe_interval_ms = int(
            max(1.0, min(float(plugin_probe_interval_seconds), 300.0)) * 1000
        )
        self._plugin_probe_attempted_at_ms = 0
        self._plugin_probe_error: str | None = None

    async def snapshot(
        self,
        *,
        user_id: str | None = None,
        active_job_id: str | None = None,
        database_ready_fn: Callable[[], Any] | None = None,
        metrics_fn: Callable[[], dict[str, Any]] | None = None,
        list_devices_fn: Callable[..., Any] | None = None,
        list_leases_fn: Callable[..., Any] | None = None,
        traffic_snapshot_fn: Callable[[], Any] | None = None,
        diagnostics_snapshot_fn: Callable[[], Any] | None = None,
        is_device_connected_fn: Callable[..., Any] | None = None,
        plugin_manifest_fn: Callable[[], Any] | None = None,
        worker_snapshot_fn: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        # Plugin identity must come from the plugin's own release manifest, not
        # from MCP clientInfo (which describes ChatGPT, not this server).
        await self.refresh_plugin_identity(manifest_fn=plugin_manifest_fn)

        backend_service = await self._probe_backend(
            database_ready_fn=database_ready_fn,
            metrics_fn=metrics_fn,
            worker_snapshot_fn=worker_snapshot_fn,
        )
        plugin_service, plugin_block = self._probe_plugin()
        extension_service = await self._probe_extension(
            user_id=user_id,
            list_devices_fn=list_devices_fn,
            list_leases_fn=list_leases_fn,
            is_device_connected_fn=is_device_connected_fn,
        )
        mcp_service = await self._probe_mcp_transport(
            traffic_snapshot_fn=traffic_snapshot_fn,
            diagnostics_snapshot_fn=diagnostics_snapshot_fn,
        )

        services = [backend_service, plugin_service, extension_service, mcp_service]
        aggregate = aggregate_bands([s["band"] for s in services])
        payload = {
            "aggregate": aggregate,
            "generated_at": _iso_now(),
            "plugin": plugin_block,
            "services": services,
            "maintain": {"active_job_id": active_job_id},
        }
        payload["fingerprint"] = hashlib.sha256(
            json.dumps(
                {
                    "aggregate": aggregate,
                    "services": [
                        {"id": s["id"], "band": s["band"], "score": s["score"]} for s in services
                    ],
                    "plugin": {
                        "band": plugin_block.get("band"),
                        "contract_version": plugin_block.get("contract_version"),
                        "tool_count": plugin_block.get("tool_count"),
                        "refresh_required": plugin_block.get("refresh_required"),
                    },
                    "active_job_id": active_job_id,
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
        return payload

    async def _probe_backend(
        self,
        *,
        database_ready_fn: Callable[[], Any] | None,
        metrics_fn: Callable[[], dict[str, Any]] | None,
        worker_snapshot_fn: Callable[[], Any] | None,
    ) -> dict[str, Any]:
        probes: list[ProbeResult] = [probe_backend_liveness(process_up=True)]
        last_error: str | None = None

        ready: bool | None = None
        try:
            if database_ready_fn is None:
                from cptr.utils.db import database_ready

                ready = bool(await database_ready())
            else:
                result = database_ready_fn()
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[misc]
                ready = bool(result)
        except Exception as exc:
            last_error = str(exc)
            ready = None
        probes.append(probe_backend_readiness(database_ready=ready, error=last_error))

        metrics: dict[str, Any] = {}
        try:
            if metrics_fn is None:
                from cptr.services.runtime_metrics import runtime_metrics

                metrics = runtime_metrics.snapshot()
            else:
                metrics = dict(metrics_fn() or {})
        except Exception as exc:
            last_error = str(exc)
            probes.append(
                ProbeResult(
                    id="backend.metrics",
                    ok=False,
                    critical=False,
                    band_hint="moderate",
                    detail=f"metrics unavailable: {exc}",
                )
            )
        else:
            event_loop = metrics.get("event_loop") or {}
            lag = event_loop.get("last_lag_ms")
            probes.append(
                probe_event_loop_lag(float(lag) if lag is not None else None)
            )
            requests = metrics.get("requests") or {}
            latency = requests.get("latency_ms") or {}
            p95 = latency.get("p95")
            samples = int(latency.get("samples") or 0)
            probes.append(
                probe_request_p95(float(p95) if p95 is not None else None, samples)
            )
            process = metrics.get("process") or {}
            open_fds = process.get("open_fds")
            probes.append(
                probe_open_fds(int(open_fds) if open_fds is not None else None)
            )

        worker_snapshot: dict[str, Any] = {}
        try:
            if worker_snapshot_fn is None:
                from cptr.services.worker_watchdog import worker_watchdog

                worker_snapshot = dict(await worker_watchdog.snapshot())
            else:
                worker_result = worker_snapshot_fn()
                if hasattr(worker_result, "__await__"):
                    worker_result = await worker_result  # type: ignore[misc]
                worker_snapshot = dict(worker_result or {})
            probes.append(probe_worker_watchdog(worker_snapshot))
        except Exception as exc:
            last_error = str(exc)
            probes.append(
                ProbeResult(
                    id="backend.worker_watchdog",
                    ok=False,
                    critical=False,
                    band_hint="moderate",
                    detail=f"worker watchdog unavailable: {exc}",
                )
            )

        band = band_from_probes(probes)
        return {
            "id": "backend",
            "name": "Computer backend",
            "band": band,
            "score": score_from_band(band),
            "probes": [p.as_dict() for p in probes],
            "stats": {
                "uptime_seconds": metrics.get("uptime_seconds"),
                "event_loop": metrics.get("event_loop"),
                "requests": metrics.get("requests"),
                "process": metrics.get("process"),
                "worker_watchdog": worker_snapshot,
            },
            "last_ok_at": _iso_now() if band == "healthy" else None,
            "last_error": last_error,
        }

    async def refresh_plugin_identity(
        self,
        *,
        manifest_fn: Callable[[], Any] | None = None,
        force: bool = False,
    ) -> PluginIdentity:
        """Refresh plugin identity from the authoritative plugin release manifest.

        The companion plugin exposes `/plugin/update`; operators provide its URL
        with CPTR_MCP_PLUGIN_UPDATE_URL or CPTR_MCP_PLUGIN_BASE_URL. When an
        authoritative probe is configured and fails, the identity fails closed.
        """
        if manifest_fn is None and not self.plugin_update_url:
            self._plugin_probe_error = None
            return self.identity_cache.get()

        if manifest_fn is None:
            now_ms = _ms_now()
            if (
                not force
                and self._plugin_probe_attempted_at_ms
                and now_ms - self._plugin_probe_attempted_at_ms
                < self.plugin_probe_interval_ms
            ):
                return self.identity_cache.get()
            self._plugin_probe_attempted_at_ms = now_ms

        try:
            if manifest_fn is not None:
                manifest = manifest_fn()
                if hasattr(manifest, "__await__"):
                    manifest = await manifest  # type: ignore[misc]
            else:
                assert self.plugin_update_url is not None
                if not self.plugin_update_url.startswith(("http://", "https://")):
                    raise ValueError("plugin update URL must use http or https")
                async with httpx.AsyncClient(
                    timeout=self.plugin_probe_timeout_seconds,
                    follow_redirects=False,
                ) as client:
                    response = await client.get(
                        self.plugin_update_url,
                        headers={"Accept": "application/json"},
                    )
                    response.raise_for_status()
                    manifest = response.json()

            if not isinstance(manifest, dict):
                raise ValueError("plugin update manifest must be an object")

            raw_tool_count = manifest.get("tool_count")
            tool_count: int | None
            if raw_tool_count is None:
                tool_count = None
            else:
                tool_count = int(raw_tool_count)
                if tool_count < 1:
                    raise ValueError("plugin tool_count must be positive")

            raw_refresh = manifest.get("refresh_required")
            refresh_required = raw_refresh if isinstance(raw_refresh, bool) else None
            identity = PluginIdentity(
                version=_bounded_text(manifest.get("version"), 64),
                contract_version=_bounded_text(manifest.get("contract_version"), 64),
                tool_count=tool_count,
                release_sha=_bounded_text(manifest.get("release_sha"), 128),
                refresh_required=refresh_required,
                source="plugin_update",
            )
            if not any(
                [identity.version, identity.contract_version, identity.tool_count is not None]
            ):
                raise ValueError("plugin update manifest contains no identity fields")

            self.identity_cache.set(identity)
            self._plugin_probe_error = None
            return self.identity_cache.get()
        except Exception as exc:
            self._plugin_probe_error = _bounded_text(exc, 240) or "plugin manifest probe failed"
            self.identity_cache.set(PluginIdentity(source="plugin_update_error"))
            return self.identity_cache.get()

    def _probe_plugin(self) -> tuple[dict[str, Any], dict[str, Any]]:
        identity = self.identity_cache.get()
        probes = probe_plugin_identity(
            identity,
            expected_contract_version=self.expected_contract_version,
            expected_tool_count=self.expected_tool_count,
        )
        band = band_from_probes(probes)
        plugin_block = {
            "version": identity.version,
            "contract_version": identity.contract_version,
            "tool_count": identity.tool_count,
            "release_sha": identity.release_sha,
            "refresh_required": identity.refresh_required,
            "source": identity.source,
            "band": band,
            "probes": [p.as_dict() for p in probes],
        }
        service = {
            "id": "plugin",
            "name": "ChatGPT Computer plugin",
            "band": band,
            "score": score_from_band(band),
            "probes": [p.as_dict() for p in probes],
            "stats": {
                "version": identity.version,
                "contract_version": identity.contract_version,
                "tool_count": identity.tool_count,
                "refresh_required": identity.refresh_required,
            },
            "last_ok_at": _iso_now() if band == "healthy" else None,
            "last_error": self._plugin_probe_error
            or (
                None
                if band != "unhealthy"
                else next((p.detail for p in probes if not p.ok), None)
            ),
        }
        return service, plugin_block

    async def _probe_extension(
        self,
        *,
        user_id: str | None,
        list_devices_fn: Callable[..., Any] | None,
        list_leases_fn: Callable[..., Any] | None,
        is_device_connected_fn: Callable[..., Any] | None,
    ) -> dict[str, Any]:
        probes: list[ProbeResult] = []
        last_error: str | None = None
        devices: list[dict[str, Any]] = []
        leases: list[dict[str, Any]] = []

        try:
            if list_devices_fn is not None:
                raw = list_devices_fn(user_id=user_id)
                if hasattr(raw, "__await__"):
                    raw = await raw  # type: ignore[misc]
                devices = list(raw or [])
            elif user_id:
                from cptr.services.browser_device_connections import (
                    browser_device_connections,
                )
                from cptr.services.browser_devices import browser_device_store

                devices = await browser_device_store.list_devices(user_id=user_id)
                for device in devices:
                    device_id = str(device.get("device_id") or "")
                    if is_device_connected_fn is not None:
                        connected = is_device_connected_fn(device_id=device_id)
                        if hasattr(connected, "__await__"):
                            connected = await connected  # type: ignore[misc]
                        device["connected"] = bool(connected)
                    else:
                        device["connected"] = await browser_device_connections.is_connected(
                            device_id=device_id
                        )
            else:
                devices = []
        except Exception as exc:
            last_error = str(exc)
            probes.append(
                ProbeResult(
                    id="extension.paired_device",
                    ok=False,
                    critical=True,
                    band_hint="unhealthy",
                    detail=f"device list failed: {exc}",
                )
            )
        else:
            probes.extend(probe_extension_devices(devices))

        try:
            if list_leases_fn is not None:
                raw_leases = list_leases_fn(user_id=user_id)
                if hasattr(raw_leases, "__await__"):
                    raw_leases = await raw_leases  # type: ignore[misc]
                leases = list(raw_leases or [])
                probes.append(probe_extension_leases(leases))
            elif user_id:
                from cptr.services.browser_devices import browser_device_store

                metrics = await browser_device_store.runtime_metrics(user_id=user_id)
                leases_by_owner = dict(metrics.get("leases_by_owner") or {})
                agent_count = int(leases_by_owner.get("agent") or 0)
                # Aggregate counts only — no session ids. Agent leases present are
                # informational moderate only when orphaned flag is supplied later.
                synthetic = [
                    {"owner": "agent", "orphaned": False}
                    for _ in range(max(0, agent_count))
                ]
                probes.append(probe_extension_leases(synthetic))
                leases = synthetic
            else:
                leases = []
                probes.append(probe_extension_leases(leases))
        except Exception as exc:
            last_error = str(exc)
            probes.append(
                ProbeResult(
                    id="extension.leases",
                    ok=False,
                    critical=False,
                    band_hint="moderate",
                    detail=f"lease probe failed: {exc}",
                )
            )

        band = band_from_probes(probes)
        return {
            "id": "extension",
            "name": "Chrome extension",
            "band": band,
            "score": score_from_band(band),
            "probes": [p.as_dict() for p in probes],
            "stats": {
                "device_count": len(devices),
                "active_connected": sum(
                    1
                    for d in devices
                    if str(d.get("status", "")).upper() == "ACTIVE" and bool(d.get("connected"))
                ),
                "lease_count": len(leases),
            },
            "last_ok_at": _iso_now() if band == "healthy" else None,
            "last_error": last_error,
        }

    async def _probe_mcp_transport(
        self,
        *,
        traffic_snapshot_fn: Callable[[], Any] | None,
        diagnostics_snapshot_fn: Callable[[], Any] | None,
    ) -> dict[str, Any]:
        diagnostics_ok = False
        traffic_ok = False
        diagnostics_error = None
        traffic_error = None
        slow_drops = 0
        stats: dict[str, Any] = {}

        try:
            if diagnostics_snapshot_fn is None:
                from cptr.services.mcp_diagnostics import mcp_diagnostics_store

                diag = await mcp_diagnostics_store.snapshot()
            else:
                diag = diagnostics_snapshot_fn()
                if hasattr(diag, "__await__"):
                    diag = await diag  # type: ignore[misc]
            diagnostics_ok = True
            stream = (diag or {}).get("stream_health") or {}
            slow_drops = int(stream.get("slow_subscriber_drops") or 0)
            stats["diagnostics"] = {"stream_health": stream}
        except Exception as exc:
            diagnostics_error = str(exc)

        try:
            if traffic_snapshot_fn is None:
                from cptr.services.mcp_traffic import mcp_traffic_store

                traffic = await mcp_traffic_store.snapshot()
            else:
                traffic = traffic_snapshot_fn()
                if hasattr(traffic, "__await__"):
                    traffic = await traffic  # type: ignore[misc]
            traffic_ok = True
            stream = (traffic or {}).get("stream_health") or {}
            slow_drops = max(slow_drops, int(stream.get("slow_subscriber_drops") or 0))
            stats["traffic"] = {
                "client_count": len((traffic or {}).get("clients") or []),
                "session_count": len((traffic or {}).get("sessions") or []),
                "stream_health": stream,
            }
        except Exception as exc:
            traffic_error = str(exc)

        probes = probe_mcp_transport(
            diagnostics_ok=diagnostics_ok,
            traffic_ok=traffic_ok,
            diagnostics_error=diagnostics_error,
            traffic_error=traffic_error,
            slow_subscriber_drops=slow_drops,
        )
        band = band_from_probes(probes)
        return {
            "id": "mcp_transport",
            "name": "MCP transport",
            "band": band,
            "score": score_from_band(band),
            "probes": [p.as_dict() for p in probes],
            "stats": stats,
            "last_ok_at": _iso_now() if band == "healthy" else None,
            "last_error": diagnostics_error or traffic_error,
        }


mcp_services_health = McpServicesHealthService()
