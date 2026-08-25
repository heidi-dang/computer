"""Bounded, authenticated managed project runtime for FlowDeck previewing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cptr.flowdeck.durable import DurableFlowDeck, RunStatus

MAX_LOG_BYTES = 128 * 1024
START_TIMEOUT = 15.0
HEALTH_TIMEOUT = 10.0
ALLOWED_PORTS = frozenset({3000, 3001, 3002, 3003, 4200, 5000, 5173, 6000, 6800, 8000, 8008, 8080, 8099, 9000})


class RuntimeContractError(ValueError):
    """Runtime input cannot be verified or is outside the bounded contract."""


@dataclass(frozen=True)
class RuntimeRequest:
    request_key: str
    workspace: str
    owner: str
    requested_port: int | None = None


@dataclass
class ManagedProcess:
    run_id: str
    workspace: str
    owner: str
    command: tuple[str, ...]
    port: int
    process: asyncio.subprocess.Process
    logs: bytearray = field(default_factory=bytearray)
    state: str = "starting"
    health: str = "unknown"
    task: asyncio.Task | None = None


def _root(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeContractError("workspace is not a directory")
    return root


def _port_available(port: int) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def discover_start_command(root: Path) -> tuple[str, ...]:
    """Discover only well-known project start commands; never evaluate shell text."""
    package = root / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            for name in ("dev", "start", "preview"):
                value = scripts.get(name)
                if isinstance(value, str) and value.strip():
                    return tuple(["npm", "run", name])
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise RuntimeContractError("package manifest is unreadable")
    for entry in ("main.py", "app.py", "server.py"):
        if (root / entry).is_file():
            return ("python", entry)
    if (root / "index.html").is_file():
        return ("python", "-m", "http.server")
    raise RuntimeContractError("no supported project start command was discovered")


def _choose_port(requested: int | None) -> int:
    if requested is not None and requested not in ALLOWED_PORTS:
        raise RuntimeContractError("requested port is not supported")
    candidates = [requested] if requested else []
    candidates.extend(sorted(ALLOWED_PORTS))
    for port in candidates:
        if port and _port_available(port):
            return port
    raise RuntimeContractError("no managed preview port is available")


class ManagedRuntimeService:
    def __init__(self) -> None:
        self._processes: dict[str, ManagedProcess] = {}
        self._lock = asyncio.Lock()

    async def start(self, request: RuntimeRequest, *, store: DurableFlowDeck) -> dict[str, Any]:
        root = _root(request.workspace)
        command = discover_start_command(root)
        async with self._lock:
            existing = await store.get_run_by_request_key(request.request_key)
            if existing:
                if existing.owner != request.owner or existing.workspace != str(root):
                    raise RuntimeContractError("runtime request key ownership mismatch")
                current = self._processes.get(existing.id)
                return await self.status(existing.id, store=store, current=current)
            port = _choose_port(request.requested_port)
            run, _ = await store.create_run(
                request_key=request.request_key, owner=request.owner, workspace=str(root), step_name="managed-runtime"
            )
            await store.start_run(run.id)
            try:
                env = os.environ.copy()
                env["PORT"] = str(port)
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(root),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception as exc:
                await store.record_event(run.id, "RUNTIME_FAILED", {"reason": str(exc), "authoritative": True})
                await store.complete_run(run.id, status=RunStatus.FAILED)
                raise RuntimeContractError("managed runtime failed to start") from exc
            managed = ManagedProcess(run.id, str(root), request.owner, command, port, process)
            self._processes[run.id] = managed
            managed.task = asyncio.create_task(self._monitor(managed, store))
            await store.record_event(
                run.id,
                "RUNTIME_START_REQUESTED",
                {"command": list(command), "port": port, "state": "starting", "source": "runtime", "authoritative": True},
            )
            return await self.status(run.id, store=store, current=managed)

    async def _monitor(self, managed: ManagedProcess, store: DurableFlowDeck) -> None:
        import httpx

        deadline = time.monotonic() + START_TIMEOUT
        if managed.process.stdout:
            asyncio.create_task(self._read_logs(managed))
        while time.monotonic() < deadline:
            if managed.process.returncode is not None:
                managed.state, managed.health = "crashed", "failed"
                await store.record_event(managed.run_id, "RUNTIME_CRASHED", {"exit_code": managed.process.returncode, "authoritative": True})
                await store.complete_run(managed.run_id, status=RunStatus.FAILED)
                return
            if not _port_available(managed.port):
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.get(
                            f"http://127.0.0.1:{managed.port}/",
                            timeout=HEALTH_TIMEOUT,
                        )
                    if response.status_code < 500:
                        managed.state, managed.health = "running", "healthy"
                        await store.record_event(managed.run_id, "RUNTIME_HEALTHY", {"port": managed.port, "status_code": response.status_code, "authoritative": True})
                        return
                except httpx.HTTPError:
                    pass
            await asyncio.sleep(0.15)
        managed.state, managed.health = "unknown", "unknown"
        await store.record_event(managed.run_id, "RUNTIME_UNKNOWN", {"reason": "health timeout", "authoritative": True})

    async def _read_logs(self, managed: ManagedProcess) -> None:
        assert managed.process.stdout is not None
        while True:
            chunk = await managed.process.stdout.read(4096)
            if not chunk:
                return
            managed.logs.extend(chunk)
            del managed.logs[:-MAX_LOG_BYTES]

    async def status(self, run_id: str, *, store: DurableFlowDeck, current: ManagedProcess | None = None) -> dict[str, Any]:
        run = await store.get_run(run_id)
        if not run:
            raise RuntimeContractError("runtime run was not found")
        current = current or self._processes.get(run_id)
        if not current:
            return {"run_id": run_id, "state": "unknown", "health": "unknown", "evidence": {"authoritative": True, "source": "verifier", "observation": "verifier_check", "reason": "managed process is not present"}}
        if current.process.returncode is not None and current.state not in {"crashed", "stopped"}:
            current.state, current.health = "crashed", "failed"
        return {
            "run_id": run_id, "state": current.state, "health": current.health,
            "port": current.port, "command": list(current.command),
            "preview_url": f"/api/flowdeck/runtime/{run_id}/preview",
            "logs": bytes(current.logs).decode("utf-8", "replace"),
            "evidence": {"authoritative": True, "source": "runtime", "observation": "verifier_check", "state": current.state, "health": current.health},
        }

    async def stop(self, run_id: str, *, store: DurableFlowDeck) -> dict[str, Any]:
        managed = self._processes.get(run_id)
        if not managed:
            return await self.status(run_id, store=store)
        if managed.task and not managed.task.done():
            managed.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await managed.task
        if managed.process.returncode is None:
            try:
                os.killpg(managed.process.pid, signal.SIGTERM)
                await asyncio.wait_for(managed.process.wait(), timeout=3)
            except (ProcessLookupError, asyncio.TimeoutError):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(managed.process.pid, signal.SIGKILL)
        managed.state, managed.health = "stopped", "stopped"
        await store.record_event(run_id, "RUNTIME_STOPPED", {"authoritative": True, "source": "runtime"})
        await store.cancel_run(
            run_id=run_id,
            owner=managed.owner,
            workspace=managed.workspace,
        )
        return await self.status(run_id, store=store, current=managed)

managed_runtime = ManagedRuntimeService()