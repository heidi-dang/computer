"""MCP Services maintain jobs: async playbooks with post-check re-probe.

Jobs never declare healthy without re-measurement. Steps are fixed playbooks;
no free-form shell from the UI.
"""

from __future__ import annotations

import asyncio
import secrets
import time
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
        self._active_job_id: str | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def active_job_id(self) -> str | None:
        with self._lock:
            return self._active_job_id

    def get_job(self, job_id: str) -> MaintainJob | None:
        with self._lock:
            return self._jobs.get(job_id)

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

    def _store_job(self, job: MaintainJob) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            while len(self._jobs) > self.max_jobs:
                oldest = next(iter(self._jobs))
                if oldest == self._active_job_id:
                    # skip active; drop next
                    keys = list(self._jobs)
                    if len(keys) < 2:
                        break
                    oldest = keys[1]
                self._jobs.pop(oldest, None)

    async def start(
        self,
        *,
        service_id: ServiceTarget = "all",
        owner_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> MaintainJob:
        with self._lock:
            if self._active_job_id and self._active_job_id in self._jobs:
                active = self._jobs[self._active_job_id]
                if active.status in ("queued", "running"):
                    # Idempotent: return active job instead of starting parallel maintain.
                    return active

            job = MaintainJob(
                job_id=_job_id() if not idempotency_key else f"msvc_{idempotency_key[:24]}",
                service_id=service_id,
                owner_id=owner_id,
            )
            if job.job_id in self._jobs and self._jobs[job.job_id].status in (
                "queued",
                "running",
            ):
                return self._jobs[job.job_id]
            self._jobs[job.job_id] = job
            self._active_job_id = job.job_id

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

        hard_failure = False
        try:
            for target in targets:
                if hard_failure and target != targets[0]:
                    step = MaintainStep(
                        step_id=f"{target}.skipped_dependency",
                        started_at=_iso_now(),
                        ended_at=_iso_now(),
                        result="skipped",
                        evidence={"reason": "upstream dependency hard failure"},
                    )
                    job.steps.append(step)
                    continue
                ok = await self._run_service_playbook(job, target)
                if not ok and target == "backend":
                    hard_failure = True

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

            if hard_failure or any(s.result == "failed" for s in job.steps):
                if any(s.result == "ok" for s in job.steps):
                    job.status = "partial"
                else:
                    job.status = "failed"
            else:
                job.status = "succeeded"
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
        finally:
            job.ended_at = _iso_now()
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None
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

        # 2. WAL checkpoint — report_only in v1 (no public operator API yet)
        step = MaintainStep(step_id="backend.wal_checkpoint", started_at=_iso_now())
        step.result = "report_only"
        step.evidence = {
            "action": "not_available",
            "note": "PRAGMA wal_autocheckpoint only; no operator-triggered checkpoint API",
        }
        step.ended_at = _iso_now()
        job.steps.append(step)
        self._emit("step", {"job_id": job.job_id, "step": step.as_dict()})

        # 3. Session reconcile — report_only unless wired later
        step = MaintainStep(step_id="backend.reconcile_sessions", started_at=_iso_now())
        step.result = "report_only"
        step.evidence = {
            "action": "not_wired",
            "note": "stale session reconcile not exposed as maintain hook in v1",
        }
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
        identity = self.health.identity_cache.get()
        has_identity = any(
            [identity.version, identity.contract_version, identity.tool_count is not None]
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
        if identity.refresh_required:
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
        else:
            step.result = "ok"
            step.evidence = {"refresh_required": False}
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
        return True

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
        return step.result != "failed"

    async def _playbook_mcp(self, job: MaintainJob) -> bool:
        step = MaintainStep(step_id="mcp.reprobe_stores", started_at=_iso_now())
        evidence: dict[str, Any] = {}
        ok = True
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
