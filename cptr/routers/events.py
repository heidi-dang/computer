"""Unified system events WebSocket: fs watching + port scanning.

Replaces the old watch.py with a single multiplexed event stream.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import platform
import sys
import threading
from pathlib import Path
from typing import Optional, NamedTuple

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers.polling import PollingObserver

from cptr.utils.terminal import manager
from cptr.utils.config import check_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])

# ── Filesystem watcher ────────────────────────────────────────────


def _create_observer():
    if platform.system() == "Darwin":
        # Prefer the native FSEvents observer on macOS – it fires only on
        # real filesystem changes instead of polling (which generates
        # constant false-positive "modified" events from stat calls).
        try:
            from watchdog.observers.fsevents import FSEventsObserver

            return FSEventsObserver()
        except ImportError:
            # If the C-extension isn't available, fall back to polling
            # with a longer interval to reduce noise.
            return PollingObserver(timeout=3)
    return Observer()


class _ChangeCollector(FileSystemEventHandler):
    """Collects fs events into a set for debounced delivery."""

    def __init__(self) -> None:
        self._changes: set[str] = set()
        self._lock = threading.Lock()

    # Paths that should never trigger a user-visible refresh
    _IGNORED_SEGMENTS = {".git", "__pycache__", ".DS_Store", "node_modules"}

    def _record(self, event: FileSystemEvent) -> None:
        path = event.src_path
        # Skip noisy internal paths that shouldn't trigger file browser refreshes
        parts = Path(path).parts
        if any(seg in self._IGNORED_SEGMENTS for seg in parts):
            return
        with self._lock:
            self._changes.add(str(Path(path).parent))
            self._changes.add(path)

    def on_created(self, event: FileSystemEvent) -> None:
        self._record(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._record(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        # Ignore directory-modified events – these fire on any child
        # change (which we already capture via on_created/on_deleted)
        # and on metadata-only updates like atime from stat calls.
        if event.is_directory:
            return
        self._record(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._record(event)
        if hasattr(event, "dest_path"):
            with self._lock:
                self._changes.add(str(Path(event.dest_path).parent))
                self._changes.add(event.dest_path)

    def drain(self) -> list[str]:
        with self._lock:
            paths = list(self._changes)
            self._changes.clear()
        return paths


class _WatchEntry(NamedTuple):
    observer: Observer
    handler: _ChangeCollector
    watch: object
    subscribers: set[asyncio.Queue[list[str]]]


_watch_registry: dict[str, _WatchEntry] = {}
_watch_registry_lock = asyncio.Lock()


async def _subscribe_to_path(path: str) -> tuple[str, asyncio.Queue[list[str]]]:
    """Subscribe this connection to one bounded, non-recursive directory watch."""
    resolved = str(Path(path).resolve())
    queue: asyncio.Queue[list[str]] = asyncio.Queue(maxsize=32)

    async with _watch_registry_lock:
        entry = _watch_registry.get(resolved)
        if entry is None:
            observer = _create_observer()
            observer.daemon = True
            handler = _ChangeCollector()
            # The file browser only needs the directory currently displayed. A
            # recursive watch on a home/workspace root can consume hundreds of
            # thousands of inotify watch descriptors and exhaust process FDs.
            watch = observer.schedule(handler, resolved, recursive=False)
            observer.start()
            entry = _WatchEntry(observer, handler, watch, set())
            _watch_registry[resolved] = entry
        entry.subscribers.add(queue)

    return resolved, queue


async def _unsubscribe_from_path(path: str, queue: asyncio.Queue[list[str]]) -> None:
    """Remove a filesystem watch subscription and stop idle observers."""
    entry: _WatchEntry | None = None
    async with _watch_registry_lock:
        current = _watch_registry.get(path)
        if current is None:
            return

        current.subscribers.discard(queue)
        if current.subscribers:
            return

        entry = _watch_registry.pop(path, None)

    if entry is None:
        return
    try:
        entry.observer.unschedule(entry.watch)
    except Exception:
        pass
    try:
        entry.observer.stop()
        # watchdog joins synchronously; never hold the asyncio event loop or
        # registry lock while waiting for its worker thread to terminate.
        await asyncio.to_thread(entry.observer.join, 2)
    except Exception:
        pass


async def _dispatch_fs_changes() -> None:
    """Fan out debounced filesystem changes from shared watchers."""
    while True:
        await asyncio.sleep(1.0)

        async with _watch_registry_lock:
            entries = list(_watch_registry.items())

        for path, entry in entries:
            paths = entry.handler.drain()
            if not paths:
                continue

            if sys.platform == "win32":
                paths = [p.replace("\\", "/") for p in paths]

            for queue in list(entry.subscribers):
                try:
                    queue.put_nowait(paths)
                except asyncio.QueueFull:
                    logger.debug("Dropped fs_change event for slow subscriber on %s", path)


_fs_dispatch_task: Optional[asyncio.Task] = None


async def _ensure_fs_dispatcher() -> None:
    global _fs_dispatch_task
    if _fs_dispatch_task is None or _fs_dispatch_task.done():
        _fs_dispatch_task = asyncio.create_task(_dispatch_fs_changes())


def _watch_error_code(exc: Exception) -> tuple[str, bool]:
    """Map watcher failures to a stable client error contract."""
    if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EMFILE}:
        return "WATCH_LIMIT", False
    return "WATCH_FAILED", True


async def _report_watch_error(ws: WebSocket, path: str, exc: Exception) -> None:
    code, retryable = _watch_error_code(exc)
    logger.warning("Failed to watch %s: %s", path, exc)
    try:
        await ws.send_json(
            {
                "type": "fs_watch_error",
                "code": code,
                "path": path,
                "retryable": retryable,
            }
        )
    except Exception:
        pass


async def _fs_watcher_loop(ws: WebSocket, initial_path: str, path_holder: dict) -> None:
    """Watch filesystem without allowing watcher failure to kill the events socket."""
    target = str(Path(initial_path).resolve())
    path_holder["current"] = target
    current_path: str | None = None
    queue: asyncio.Queue[list[str]] | None = None
    failed_path: str | None = None

    await _ensure_fs_dispatcher()

    async def subscribe(path: str) -> bool:
        nonlocal current_path, queue, failed_path
        try:
            current_path, queue = await _subscribe_to_path(path)
            failed_path = None
            path_holder["current"] = current_path
            return True
        except Exception as exc:
            failed_path = path
            current_path = None
            queue = None
            path_holder["current"] = path
            await _report_watch_error(ws, path, exc)
            return False

    await subscribe(target)

    try:
        while True:
            # Changing path explicitly resets the per-path circuit. A failed
            # path is not retried just because the unified WebSocket remains live.
            new_path = path_holder.pop("pending", None)
            if new_path:
                resolved = str(Path(new_path).resolve())
                if resolved != path_holder["current"] and Path(resolved).is_dir():
                    if current_path is not None and queue is not None:
                        await _unsubscribe_from_path(current_path, queue)
                        current_path = None
                        queue = None
                    if resolved != failed_path and await subscribe(resolved):
                        logger.info("FS watch path changed to %s", resolved)

            if queue is None:
                await asyncio.sleep(0.25)
                continue

            try:
                paths = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                await ws.send_json({"type": "fs_change", "paths": paths})
            except Exception:
                break
    finally:
        if current_path is not None and queue is not None:
            await _unsubscribe_from_path(current_path, queue)


# ── Port scanning ─────────────────────────────────────────────────

# System processes to ignore
_IGNORED_PROCESSES = {
    "sshd",
    "cupsd",
    "mDNSResponder",
    "rapportd",
    "systemd-resolve",
    "avahi-daemon",
    "dnsmasq",
    "launchd",
    "systemd",
    "ntpd",
    "bluetoothd",
    "AirPlayXPCHelper",
    "ControlCenter",
}

# Ports that are almost certainly system services
_SYSTEM_PORTS = {22, 53, 80, 443, 631, 5353}


async def _get_ppid(pid: int) -> int:
    """Get parent PID. Cross-platform."""
    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "wmic",
                "process",
                "where",
                f"ProcessId={pid}",
                "get",
                "ParentProcessId",
                "/value",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            for line in stdout.decode(errors="replace").splitlines():
                if line.startswith("ParentProcessId="):
                    return int(line.split("=", 1)[1])
            return 0
        elif sys.platform == "linux":

            def _read_ppid():
                with open(f"/proc/{pid}/stat") as f:
                    return int(f.read().split()[3])

            return await asyncio.to_thread(_read_ppid)
        else:
            proc = await asyncio.create_subprocess_exec(
                "ps",
                "-o",
                "ppid=",
                "-p",
                str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            return int(stdout.decode(errors="replace").strip())
    except Exception:
        return 0


async def _find_session_for_pid(pid: int) -> Optional[str]:
    """Walk up the process tree to find which terminal session spawned this PID."""
    current = pid
    visited: set[int] = set()
    while current > 1 and current not in visited:
        visited.add(current)
        for session in manager._sessions.values():
            # Unix: match by child PID
            if session._pid == current:
                return session.session_id
            # Windows: winpty process has a .pid attribute
            if session._process is not None:
                try:
                    if getattr(session._process, "pid", None) == current:
                        return session.session_id
                except Exception:
                    pass
        current = await _get_ppid(current)
    return None


async def _get_process_name(pid: int) -> str:
    """Get process name from PID."""
    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "tasklist",
                "/FI",
                f"PID eq {pid}",
                "/FO",
                "CSV",
                "/NH",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            line = stdout.decode(errors="replace").strip()
            if line and line.startswith('"'):
                return line.split('"')[1]
            return "unknown"
        elif sys.platform == "linux":

            def _read_comm():
                with open(f"/proc/{pid}/comm") as f:
                    return f.read().strip()

            return await asyncio.to_thread(_read_comm)
        else:
            proc = await asyncio.create_subprocess_exec(
                "ps",
                "-o",
                "comm=",
                "-p",
                str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            name = stdout.decode(errors="replace").strip()
            # macOS returns full path, extract basename
            return os.path.basename(name) if name else "unknown"
    except Exception:
        return "unknown"


async def _scan_ports_darwin() -> list[dict]:
    """Scan listening ports on macOS using lsof."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "lsof",
            "-iTCP",
            "-sTCP:LISTEN",
            "-nP",
            "-F",
            "pcn",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return []

        ports = []
        current_pid = 0
        current_process = ""
        for line in stdout.decode(errors="replace").splitlines():
            if line.startswith("p"):
                current_pid = int(line[1:])
            elif line.startswith("c"):
                current_process = line[1:]
            elif line.startswith("n"):
                # Format: n*:PORT or nhost:PORT
                addr = line[1:]
                if ":" in addr:
                    port_str = addr.rsplit(":", 1)[1]
                    try:
                        port = int(port_str)
                        ports.append(
                            {
                                "port": port,
                                "pid": current_pid,
                                "process": current_process,
                            }
                        )
                    except ValueError:
                        pass
        return ports
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
        return []


def _linux_socket_pid_map() -> dict[int, int]:
    """Build socket-inode -> PID once for an entire Linux port scan."""
    result: dict[int, int] = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                fd_dir = f"/proc/{entry}/fd"
                for fd in os.listdir(fd_dir):
                    try:
                        link = os.readlink(f"{fd_dir}/{fd}")
                    except (OSError, ValueError):
                        continue
                    if not link.startswith("socket:[") or not link.endswith("]"):
                        continue
                    try:
                        inode = int(link[8:-1])
                    except ValueError:
                        continue
                    result.setdefault(inode, pid)
            except (OSError, PermissionError):
                continue
    except Exception:
        pass
    return result


async def _scan_ports_linux() -> list[dict]:
    """Scan Linux listening ports with one /proc FD traversal per scan."""
    ports = []
    try:

        def _read_proc_net():
            with open("/proc/net/tcp") as f:
                return f.readlines()[1:]  # skip header

        lines, pid_by_inode = await asyncio.gather(
            asyncio.to_thread(_read_proc_net),
            asyncio.to_thread(_linux_socket_pid_map),
        )
        process_names: dict[int, str] = {}
        for line in lines:
            parts = line.split()
            if parts[3] != "0A":  # LISTEN state
                continue
            local = parts[1]
            port = int(local.split(":")[1], 16)
            inode = int(parts[9])
            pid = pid_by_inode.get(inode, 0)
            if pid and pid not in process_names:
                process_names[pid] = await _get_process_name(pid)
            ports.append(
                {
                    "port": port,
                    "pid": pid,
                    "process": process_names.get(pid, "unknown"),
                }
            )
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
    return ports


async def _scan_ports_windows() -> list[dict]:
    """Scan listening ports on Windows using netstat."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "netstat",
            "-ano",
            "-p",
            "TCP",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        ports = []
        for line in stdout.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTENING" in parts:
                local = parts[1]
                if ":" in local:
                    port_str = local.rsplit(":", 1)[1]
                    try:
                        port = int(port_str)
                        pid = int(parts[-1])
                        ports.append(
                            {
                                "port": port,
                                "pid": pid,
                                "process": await _get_process_name(pid),
                            }
                        )
                    except ValueError:
                        pass
        return ports
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
        return []


async def _scan_ports() -> list[dict]:
    """Scan listening ports. Cross-platform."""
    system = platform.system()
    if system == "Darwin":
        raw = await _scan_ports_darwin()
    elif system == "Linux":
        raw = await _scan_ports_linux()
    elif system == "Windows":
        raw = await _scan_ports_windows()
    else:
        return []

    # Get our own PID to filter ourselves out
    our_pid = os.getpid()

    # Deduplicate by port and filter
    seen: set[int] = set()
    filtered = []
    for entry in raw:
        port = entry["port"]
        if port in seen:
            continue
        seen.add(port)

        # Skip system ports and our own process
        if port in _SYSTEM_PORTS:
            continue
        if entry["pid"] == our_pid:
            continue
        if entry["process"] in _IGNORED_PROCESSES:
            continue

        # Session attribution: only include ports spawned by our terminals
        if entry["pid"]:
            session_id = await _find_session_for_pid(entry["pid"])
            if session_id:
                entry["session_id"] = session_id
                filtered.append(entry)
            # else: not from a cptr terminal, skip
        # pid=0 or no pid: skip

    return filtered


_PORT_SCAN_INTERVAL_SECONDS = 3.0
_port_subscribers: set[asyncio.Queue[dict]] = set()
_port_subscribers_lock = asyncio.Lock()
_port_monitor_task: asyncio.Task | None = None
_port_snapshot: dict[int, dict] = {}


def _port_added_event(info: dict) -> dict:
    return {
        "type": "port_added",
        "port": info["port"],
        "pid": info["pid"],
        "process": info["process"],
        "session_id": info.get("session_id"),
    }


def _fanout_port_events(events: list[dict], subscribers: list[asyncio.Queue[dict]]) -> None:
    for event in events:
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug("Dropped port event for slow subscriber")


async def _port_monitor_loop() -> None:
    """Scan ports once per interval and fan out deltas to all WebSockets."""
    global _port_snapshot
    try:
        while True:
            await asyncio.sleep(_PORT_SCAN_INTERVAL_SECONDS)
            try:
                current_ports = {p["port"]: p for p in await _scan_ports()}
            except Exception as e:
                logger.warning("Port scan error: %s", e)
                continue

            async with _port_subscribers_lock:
                previous = _port_snapshot
                pending_events = [
                    _port_added_event(info)
                    for port, info in current_ports.items()
                    if port not in previous
                ]
                pending_events.extend(
                    {"type": "port_removed", "port": port}
                    for port in previous
                    if port not in current_ports
                )
                # Snapshot publication and subscriber capture are atomic: a new
                # subscriber either receives these deltas or replays the new snapshot.
                _port_snapshot = current_ports
                subscribers = list(_port_subscribers)

            _fanout_port_events(pending_events, subscribers)
    except asyncio.CancelledError:
        raise


async def _subscribe_port_events() -> asyncio.Queue[dict]:
    """Subscribe to the singleton port monitor and replay its current snapshot."""
    global _port_monitor_task
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=64)
    async with _port_subscribers_lock:
        _port_subscribers.add(queue)
        snapshot = list(_port_snapshot.values())
        if _port_monitor_task is None or _port_monitor_task.done():
            _port_monitor_task = asyncio.create_task(_port_monitor_loop())
    for info in snapshot:
        queue.put_nowait(_port_added_event(info))
    return queue


async def _unsubscribe_port_events(queue: asyncio.Queue[dict]) -> None:
    """Detach a client and stop the shared monitor when the last client leaves."""
    global _port_monitor_task, _port_snapshot
    task: asyncio.Task | None = None
    async with _port_subscribers_lock:
        _port_subscribers.discard(queue)
        if not _port_subscribers and _port_monitor_task is not None:
            task = _port_monitor_task
            _port_monitor_task = None
            _port_snapshot = {}
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _port_scanner_loop(ws: WebSocket) -> None:
    """Forward events from the singleton backend port monitor to one client."""
    queue = await _subscribe_port_events()
    try:
        while True:
            event = await queue.get()
            try:
                await ws.send_json(event)
            except Exception:
                return
    finally:
        await _unsubscribe_port_events(queue)


# ── Receive loop ──────────────────────────────────────────────────


async def _receive_loop(ws: WebSocket, path_holder: dict) -> None:
    """Handle incoming messages from the client."""
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "watch_path":
                new_path = msg.get("path")
                if new_path:
                    path_holder["pending"] = new_path
    except (WebSocketDisconnect, Exception):
        pass


# ── WebSocket endpoint ────────────────────────────────────────────


@router.websocket("/ws")
async def events_ws(
    websocket: WebSocket, path: str = Query("/", description="Initial fs watch path")
):
    """Unified system events WebSocket.

    Pushes:
      - {"type": "fs_change", "paths": [...]}
      - {"type": "fs_watch_error", "code": "...", "path": "...", "retryable": bool}
      - {"type": "port_added", "port": N, "pid": N, "process": "...", "session_id": "..." | null}
      - {"type": "port_removed", "port": N}

    Receives:
      - {"type": "watch_path", "path": "..."}: change fs watch directory
    """
    target = Path(path).resolve()
    if not target.is_dir():
        await websocket.close(code=4000, reason="Not a directory")
        return

    # Auth check for WebSocket
    client_host = websocket.client.host if websocket.client else "127.0.0.1"
    token = websocket.cookies.get("cptr_session") or websocket.query_params.get("token")
    auth = check_access(client_host=client_host, jwt_token=token)
    if auth is None:
        await websocket.close(code=4001, reason="unauthorized")
        return

    await websocket.accept()
    logger.info(f"Events WS connected, watching {target}")

    path_holder: dict = {}

    fs_task = asyncio.create_task(_fs_watcher_loop(websocket, str(target), path_holder))
    port_task = asyncio.create_task(_port_scanner_loop(websocket))
    recv_task = asyncio.create_task(_receive_loop(websocket, path_holder))

    try:
        done, pending = await asyncio.wait(
            [fs_task, port_task, recv_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.error(f"Events WS error: {e}")
    finally:
        logger.info("Events WS disconnected")
