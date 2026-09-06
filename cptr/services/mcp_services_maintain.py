"""MCP Services maintain jobs: async playbooks with post-check re-probe.

Jobs never declare healthy without re-measurement. Steps are fixed playbooks;
no free-form shell from the UI.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Literal

from cptr.services.mcp_services_health import (
    McpServicesHealthService,
    mcp_services_health,
)

JobStatus = Literal["queued", "running", "succeeded", "failed", "partial"]
StepResult = Literal["ok", "skipped", "failed", "report_only"]
ServiceTarget = Literal["all", "backend", "plugin", "extension", "mcp_transport"]

MAX_JOBS = 50


class MaintainConflictError(RuntimeError):
    def __init__(self, active_job_id: str) -> None:
        super().__init__(f"maintain job already active: {active_job_id}")
        self.active_job_id = active_job_id


class MaintainIdempotencyConflictError(RuntimeError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"idempotency key is already bound to maintain job: {job_id}")
        self.job_id = job_id


def _owner_key(owner_id: str | None) -> str:
    return owner_id or "__system__"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _job_id() -> str:
    return f"msvc_{secrets.token_hex(8)}"


@dataclass
class MaintainStep:
    step_id: str
    started_at: str | None = None
    ended_at: str | None = None
    result: StepResult | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "result": self.result,
            "evidence": self.evidence,
        }


@dataclass
class MaintainJob:
    job_id: str
    service_id: ServiceTarget
    status: JobStatus = "queued"
    started_at: str | None = None
    ended_at: str | None = None
    steps: list[MaintainStep] = field(default_factory=list)
    post_band: str | None = None
    post_aggregate: str | None = None
    error: str | None = None
    owner_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "service_id": self.service_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "steps": [s.as_dict() for s in self.steps],
            "post_band": self.post_band,
            "post_aggregate": self.post_aggregate,
            "error": self.error,
        }


class McpServicesMaintainService:
    def __init__(
        self,
        *,
        health: McpServicesHealthService | None = None,
        max_jobs: int = MAX_JOBS,
    ) -> None:
        self.health = health or mcp_services_health
        self.max_jobs = max(1, int(max_jobs))
        self._lock = Lock()
        self._jobs: dict[str, MaintainJob] = {}
        self._active_job_ids: dict[str, str] = {}
        self._idempotency_jobs: dict[tuple[str, str], str] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def active_job_id(self, *, owner_id: str | None = None) -> str | None:
        with self._lock:
            return self._active_job_ids.get(_owner_key(owner_id))

    def get_job(
        self, job_id: str, *, owner_id: str | None = None
    ) -> MaintainJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if owner_id is not None and job.owner_id != owner_id:
                return None
            return job

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, **data}
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

    def _prune_jobs_locked(self) -> None:
        while len(self._jobs) > self.max_jobs:
            removable = next(
                (
                    job_id
                    for job_id, job in self._jobs.items()
                    if job.status not in ("queued", "running")
                ),
                None,
            )
            if removable is None:
                break
            self._jobs.pop(removable, None)
            stale_keys = [
                key
                for key, mapped_job_id in self._idempotency_jobs.items()
                if mapped_job_id == removable
            ]
            for key in stale_keys:
                self._idempotency_jobs.pop(key, None)

    async def start(
        self,
        *,
        service_id: ServiceTarget = "all",
        owner_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> MaintainJob:
        owner_key = _owner_key(owner_id)
        normalized_key = (idempotency_key or "").strip()[:128]
        with self._lock:
            if normalized_key:
                mapped_id = self._idempotency_jobs.get((owner_key, normalized_key))
                if mapped_id:
                    mapped = self._jobs.get(mapped_id)
                    if mapped is not None:
                        if mapped.service_id != service_id:
                            raise MaintainIdempotencyConflictError(mapped.job_id)
                        return mapped
                    self._idempotency_jobs.pop((owner_key, normalized_key), None)

            active_id = self._active_job_ids.get(owner_key)
            if active_id:
                active = self._jobs.get(active_id)
                if active is not None and active.status in ("queued", "running"):
                    raise MaintainConflictError(active.job_id)
                self._active_job_ids.pop(owner_key, None)

            job = MaintainJob(
                job_id=_job_id(),
                service_id=service_id,
                owner_id=owner_id,
            )
            self._jobs[job.job_id] = job
            self._active_job_ids[owner_key] = job.job_id
            if normalized_key:
                self._idempotency_jobs[(owner_key, normalized_key)] = job.job_id
            self._prune_jobs_locked()

        asyncio.create_task(self._run_job(job.job_id))
        return job

    async def _run_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            return
        job.status = "running"
        job.started_at = _iso_now()
        self._emit("job_started", {"job_id": job_id, "service_id": job.service_id})

        targets: list[str]
        if job.service_id == "all":
            targets = ["backend", "plugin", "extension", "mcp_transport"]
        else:
            targets = [job.service_id]

        try:
            for target in targets:
                # Services are maintained independently. A backend playbook failure
                # must not suppress safe plugin/extension/MCP maintenance work.
                await self._run_service_playbook(job, target)

            # Mandatory post-check: band only from probes.
            snapshot = await self.health.snapshot(
                user_id=job.owner_id,
                active_job_id=job.job_id,
            )
            job.post_aggregate = str(snapshot.get("aggregate"))
            if job.service_id == "all":
                job.post_band = job.post_aggregate
            else:
                match = next(
                    (
                        s
                        for s in snapshot.get("services", [])
                        if isinstance(s, dict) and s.get("id") == job.service_id
                    ),
                    None,
                )
                job.post_band = str(match.get("band")) if match else job.post_aggregate

            has_failed_step = any(s.result == "failed" for s in job.steps)
            has_ok_step = any(s.result == "ok" for s in job.steps)
            # Post-health is authoritative: unhealthy can never be reported as
            # success/partial even when some maintenance actions completed.
            if job.post_band == "unhealthy":
                job.status = "failed"
            elif has_failed_step:
                job.status = "partial" if has_ok_step else "failed"
            elif job.post_band == "healthy":
                job.status = "succeeded"
            elif job.post_band == "moderate":
                job.status = "partial"
            else:
                job.status = "failed"
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
        finally:
            job.ended_at = _iso_now()
            with self._lock:
                owner_key = _owner_key(job.owner_id)
                if self._active_job_ids.get(owner_key) == job_id:
                    self._active_job_ids.pop(owner_key, None)
            self._emit(
                "job_finished",
                {
                    "job_id": job_id,
                    "status": job.status,
                    "post_band": job.post_band,
                    "post_aggregate": job.post_aggregate,
                },
            )

    async def _run_service_playbook(self, job: MaintainJob, service_id: str) -> bool:
        if service_id == "backend":
            return await self._playbook_backend(job)
        if service_id == "plugin":
            return await self._playbook_plugin(job)
        if service_id == "extension":
            return await self._playbook_extension(job)
        if service_id == "mcp_transport":
            return await self._playbook_mcp(job)
        step = MaintainStep(
            step_id=f"{service_id}.unknown",
            started_at=_iso_now(),
            ended_at=_iso_now(),
            result="failed",
            evidence={"reason": "unknown service"},
        )
        job.steps.append(step)
        return False

    async def _playbook_backend(self, job: MaintainJob) -> bool:
        # 1. Re-check readiness
        step = MaintainStep(step_id="backend.recheck_readiness", started_at=_iso_now())
        try:
            from cptr.utils.db import database_ready

            ready = bool(await database_ready())
            step.result = "ok" if ready else "failed"
            step.evidence = {"ready": ready}
        except Exception as exc:
            step.result = "failed"
            step.evidence = {"error": str(exc)}
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})
        if step.result == "failed":
            return False

        # 2. Run a bounded, non-blocking WAL checkpoint. PASSIVE never waits for
        # readers and reports whether a busy reader prevented full checkpointing.
        step = MaintainStep(step_id="backend.wal_checkpoint", started_at=_iso_now())
        try:
            from sqlalchemy import text

            from cptr.utils.db import get_engine

            async with get_engine().connect() as connection:
                result = await connection.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))
                row = result.first()
            busy = int(row[0]) if row is not None else 0
            log_frames = int(row[1]) if row is not None and len(row) > 1 else 0
            checkpointed_frames = int(row[2]) if row is not None and len(row) > 2 else 0
            step.result = "ok" if busy == 0 else "report_only"
            step.evidence = {
                "mode": "PASSIVE",
                "busy": busy,
                "log_frames": log_frames,
                "checkpointed_frames": checkpointed_frames,
            }
        except Exception as exc:
            step.result = "failed"
            step.evidence = {"error": str(exc)}
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})

        # 3. Reconcile stale command completion/capacity state and reap only
        # already-completed sessions that satisfy normal registry retention policy.
        step = MaintainStep(step_id="backend.reconcile_sessions", started_at=_iso_now())
        try:
            from cptr.services.execution_manager import command_session_registry

            reconciled_sessions = command_session_registry.reconcile()
            reconciled_reservations = command_session_registry.reconcile_launch_reservations()
            reaped = command_session_registry.reap()
            step.result = "ok"
            step.evidence = {
                "reconciled_sessions": reconciled_sessions,
                "released_orphan_reservations": reconciled_reservations,
                "reaped_completed_sessions": len(reaped),
                "active_sessions": command_session_registry.active_count(),
                "capacity_used": command_session_registry.capacity_count(),
            }
        except Exception as exc:
            step.result = "failed"
            step.evidence = {"error": str(exc)}
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})

        # 4. Re-probe metrics
        step = MaintainStep(step_id="backend.reprobe_metrics", started_at=_iso_now())
        try:
            from cptr.services.runtime_metrics import runtime_metrics

            snap = runtime_metrics.snapshot()
            step.result = "ok"
            step.evidence = {
                "event_loop": snap.get("event_loop"),
                "uptime_seconds": snap.get("uptime_seconds"),
            }
        except Exception as exc:
            step.result = "failed"
            step.evidence = {"error": str(exc)}
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})
        return step.result != "failed"

    async def _playbook_plugin(self, job: MaintainJob) -> bool:
        step = MaintainStep(step_id="plugin.probe_identity", started_at=_iso_now())
        identity = await self.health.refresh_plugin_identity(force=True)
        has_identity = bool(
            identity.version
            and identity.contract_version
            and identity.tool_count is not None
        )
        step.result = "ok" if has_identity else "failed"
        step.evidence = {
            "version": identity.version,
            "contract_version": identity.contract_version,
            "tool_count": identity.tool_count,
            "refresh_required": identity.refresh_required,
            "source": identity.source,
        }
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})

        step = MaintainStep(step_id="plugin.refresh_required", started_at=_iso_now())
        if identity.refresh_required is True:
            step.result = "report_only"
            step.evidence = {
                "action": "host_refresh_required",
                "path": [
                    "Settings",
                    "Apps / Plugins",
                    "CPTR Computer",
                    "Manage / Action control",
                    "Refresh",
                ],
            }
        elif identity.refresh_required is False:
            step.result = "ok"
            step.evidence = {"refresh_required": False}
        else:
            step.result = "report_only"
            step.evidence = {
                "action": "unknown",
                "note": "plugin manifest did not report refresh_required",
            }
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})

        step = MaintainStep(step_id="plugin.workbench", started_at=_iso_now())
        step.result = "report_only"
        step.evidence = {
            "action": "not_available",
            "note": "cannot force workbench subsystem restart from maintain",
        }
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})
        return has_identity

    async def _playbook_extension(self, job: MaintainJob) -> bool:
        step = MaintainStep(step_id="extension.list_devices", started_at=_iso_now())
        devices: list[dict[str, Any]] = []
        try:
            if job.owner_id:
                from cptr.services.browser_device_connections import (
                    browser_device_connections,
                )
                from cptr.services.browser_devices import browser_device_store

                devices = await browser_device_store.list_devices(user_id=job.owner_id)
                for device in devices:
                    device["connected"] = await browser_device_connections.is_connected(
                        device_id=str(device.get("device_id") or "")
                    )
            step.result = "ok"
            step.evidence = {
                "count": len(devices),
                "active": sum(
                    1 for d in devices if str(d.get("status", "")).upper() == "ACTIVE"
                ),
                "connected": sum(1 for d in devices if bool(d.get("connected"))),
            }
        except Exception as exc:
            step.result = "failed"
            step.evidence = {"error": str(exc)}
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})
        devices_ok = step.result != "failed"

        # Clear stuck leases — report_only without session ids/epochs in v1
        step = MaintainStep(step_id="extension.clear_stuck_leases", started_at=_iso_now())
        step.result = "report_only"
        step.evidence = {
            "action": "policy_gated",
            "note": (
                "transfer_lease agent→none requires session_id + expected_epoch; "
                "maintain lists status only in v1"
            ),
        }
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})
        return devices_ok

    async def _playbook_mcp(self, job: MaintainJob) -> bool:
        step = MaintainStep(step_id="mcp.expire_stale_sessions", started_at=_iso_now())
        try:
            from cptr.services.mcp_traffic import mcp_traffic_store

            expired = await mcp_traffic_store.expire_stale_sessions()
            step.result = "ok"
            step.evidence = {"expired_sessions": expired}
        except Exception as exc:
            step.result = "failed"
            step.evidence = {"error": str(exc)}
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})
        maintenance_ok = step.result != "failed"

        step = MaintainStep(step_id="mcp.reprobe_stores", started_at=_iso_now())
        evidence: dict[str, Any] = {}
        ok = maintenance_ok
        try:
            from cptr.services.mcp_diagnostics import mcp_diagnostics_store

            diag = await mcp_diagnostics_store.snapshot()
            evidence["diagnostics_ok"] = True
            evidence["diagnostics_stream"] = (diag or {}).get("stream_health")
        except Exception as exc:
            ok = False
            evidence["diagnostics_ok"] = False
            evidence["diagnostics_error"] = str(exc)
        try:
            from cptr.services.mcp_traffic import mcp_traffic_store

            traffic = await mcp_traffic_store.snapshot()
            evidence["traffic_ok"] = True
            evidence["traffic_stream"] = (traffic or {}).get("stream_health")
        except Exception as exc:
            ok = False
            evidence["traffic_ok"] = False
            evidence["traffic_error"] = str(exc)
        step.result = "ok" if ok else "failed"
        step.evidence = evidence
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})
        return ok


mcp_services_maintain = McpServicesMaintainService()
