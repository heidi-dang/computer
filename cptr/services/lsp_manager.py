"""Bounded workspace-scoped Language Server Protocol process manager.

This module gives CPTR a first-class LSP lifecycle without allowing callers to
supply arbitrary executables. Server IDs map to an administrator-controlled
registry (with an optional JSON environment override), processes are owned by a
CPTR user/workspace, and JSON-RPC frames are size/time bounded.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_LSP_SESSIONS_PER_USER = 8
MAX_LSP_FRAME_BYTES = 2 * 1024 * 1024
MAX_LSP_HEADER_BYTES = 64 * 1024
MAX_LSP_STDERR_BYTES = 64 * 1024
DEFAULT_LSP_TIMEOUT_SECONDS = 15.0

_DEFAULT_SERVER_COMMANDS: dict[str, list[str]] = {
    "typescript": ["typescript-language-server", "--stdio"],
    "pyright": ["pyright-langserver", "--stdio"],
    "rust-analyzer": ["rust-analyzer"],
    "gopls": ["gopls"],
    "clangd": ["clangd"],
}


class LspError(RuntimeError):
    """Typed LSP lifecycle/protocol failure."""


@dataclass
class _LspSession:
    lsp_id: str
    server_id: str
    root: Path
    user_id: str
    process: asyncio.subprocess.Process
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    next_request_id: int = 1
    reader_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    stderr_tail: bytearray = field(default_factory=bytearray)


class LspManager:
    def __init__(self, *, server_commands: dict[str, list[str]] | None = None) -> None:
        self._server_commands = server_commands or self._configured_commands()
        self._sessions: dict[str, _LspSession] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _configured_commands() -> dict[str, list[str]]:
        raw = os.getenv("CPTR_LSP_SERVERS_JSON", "").strip()
        if not raw:
            return {key: list(value) for key, value in _DEFAULT_SERVER_COMMANDS.items()}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LspError("CPTR_LSP_SERVERS_JSON must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise LspError("CPTR_LSP_SERVERS_JSON must be an object")
        commands: dict[str, list[str]] = {}
        for key, value in parsed.items():
            if not isinstance(key, str) or not key or not isinstance(value, list) or not value:
                raise LspError("invalid LSP server registry entry")
            if not all(isinstance(item, str) and item for item in value):
                raise LspError("LSP server command argv must contain non-empty strings")
            commands[key] = list(value)
        return commands

    @staticmethod
    def _resolve_argv(argv: list[str]) -> list[str] | None:
        executable = argv[0]
        if os.path.isabs(executable):
            if not Path(executable).is_file():
                return None
            resolved = executable
        else:
            resolved = shutil.which(executable)
            if not resolved:
                return None
        return [resolved, *argv[1:]]

    def discover(self) -> dict[str, Any]:
        servers = []
        for server_id, argv in sorted(self._server_commands.items()):
            resolved = self._resolve_argv(argv)
            servers.append(
                {
                    "server_id": server_id,
                    "available": resolved is not None,
                    "executable": Path(argv[0]).name,
                }
            )
        return {"servers": servers}

    def _owned(self, lsp_id: str, user_id: str) -> _LspSession:
        session = self._sessions.get(lsp_id)
        if session is None or session.user_id != user_id:
            raise LspError("language server session not found")
        return session

    def validate_scope(self, *, lsp_id: str, user_id: str, workspace_root: Path) -> None:
        session = self._owned(lsp_id, user_id)
        allowed = workspace_root.resolve()
        try:
            session.root.relative_to(allowed)
        except ValueError as exc:
            raise LspError("language server session is outside the selected workspace") from exc

    async def start(
        self,
        *,
        server_id: str,
        root: Path,
        user_id: str,
        env: dict[str, str] | None = None,
        preexec_fn=None,
    ) -> dict[str, Any]:
        argv = self._server_commands.get(server_id)
        if argv is None:
            raise LspError(f"unknown language server: {server_id}")
        resolved = self._resolve_argv(argv)
        if resolved is None:
            raise LspError(f"language server is not installed: {server_id}")
        root = root.resolve()
        if not root.is_dir():
            raise LspError("language server root is not a directory")

        async with self._lock:
            active = sum(1 for session in self._sessions.values() if session.user_id == user_id)
            if active >= MAX_LSP_SESSIONS_PER_USER:
                raise LspError("too many active language server sessions")

        kwargs: dict[str, Any] = {
            "cwd": str(root),
            "env": env,
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
            if preexec_fn is not None:
                kwargs["preexec_fn"] = preexec_fn
        process = await asyncio.create_subprocess_exec(*resolved, **kwargs)
        lsp_id = f"lsp_{uuid.uuid4().hex[:16]}"
        session = _LspSession(
            lsp_id=lsp_id,
            server_id=server_id,
            root=root,
            user_id=user_id,
            process=process,
        )
        self._sessions[lsp_id] = session
        session.reader_task = asyncio.create_task(
            self._reader_loop(session), name=f"cptr-lsp-reader-{lsp_id}"
        )
        session.stderr_task = asyncio.create_task(
            self._stderr_loop(session), name=f"cptr-lsp-stderr-{lsp_id}"
        )
        try:
            initialize = await self.request(
                lsp_id=lsp_id,
                user_id=user_id,
                method="initialize",
                params={
                    "processId": os.getpid(),
                    "rootUri": root.as_uri(),
                    "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
                    "capabilities": {},
                    "clientInfo": {"name": "CPTR", "version": "1"},
                },
                timeout_seconds=DEFAULT_LSP_TIMEOUT_SECONDS,
            )
            await self.notify(lsp_id=lsp_id, user_id=user_id, method="initialized", params={})
        except Exception:
            await self._terminate(session)
            self._sessions.pop(lsp_id, None)
            raise
        return {
            "lsp_id": lsp_id,
            "server_id": server_id,
            "root": ".",
            "status": "RUNNING",
            "pid": process.pid,
            "capabilities": (initialize.get("result") or {}).get("capabilities", {})
            if isinstance(initialize.get("result"), dict)
            else {},
        }

    async def request(
        self,
        *,
        lsp_id: str,
        user_id: str,
        method: str,
        params: Any = None,
        timeout_seconds: float = DEFAULT_LSP_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        session = self._owned(lsp_id, user_id)
        if session.process.returncode is not None:
            raise LspError("language server process has exited")
        request_id = session.next_request_id
        session.next_request_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        session.pending[request_id] = future
        try:
            await self._send(
                session, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            response = await asyncio.wait_for(future, timeout=max(0.1, min(timeout_seconds, 60.0)))
        except asyncio.TimeoutError as exc:
            raise LspError(f"language server request timed out: {method}") from exc
        finally:
            session.pending.pop(request_id, None)
        return response

    async def notify(self, *, lsp_id: str, user_id: str, method: str, params: Any = None) -> None:
        session = self._owned(lsp_id, user_id)
        await self._send(session, {"jsonrpc": "2.0", "method": method, "params": params})

    async def stop(self, *, lsp_id: str, user_id: str) -> dict[str, Any]:
        session = self._owned(lsp_id, user_id)
        try:
            if session.process.returncode is None:
                try:
                    await self.request(
                        lsp_id=lsp_id,
                        user_id=user_id,
                        method="shutdown",
                        params=None,
                        timeout_seconds=2.0,
                    )
                    await self.notify(lsp_id=lsp_id, user_id=user_id, method="exit", params=None)
                except Exception:
                    pass
        finally:
            await self._terminate(session)
            self._sessions.pop(lsp_id, None)
        return {"lsp_id": lsp_id, "server_id": session.server_id, "status": "STOPPED"}

    async def _send(self, session: _LspSession, message: dict[str, Any]) -> None:
        stdin = session.process.stdin
        if stdin is None:
            raise LspError("language server stdin is unavailable")
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_LSP_FRAME_BYTES:
            raise LspError("language server request exceeds the frame limit")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        async with session.write_lock:
            stdin.write(frame)
            await stdin.drain()

    async def _reader_loop(self, session: _LspSession) -> None:
        stdout = session.process.stdout
        if stdout is None:
            return
        try:
            while True:
                header = await stdout.readuntil(b"\r\n\r\n")
                if len(header) > MAX_LSP_HEADER_BYTES:
                    raise LspError("language server response header exceeds the limit")
                length = None
                for line in header.decode("ascii", errors="replace").split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1].strip())
                        break
                if length is None or length < 0 or length > MAX_LSP_FRAME_BYTES:
                    raise LspError("invalid language server Content-Length")
                payload = json.loads((await stdout.readexactly(length)).decode("utf-8"))
                if not isinstance(payload, dict):
                    continue
                response_id = payload.get("id")
                method = payload.get("method")
                if response_id is not None and isinstance(method, str):
                    await self._reply_to_server_request(session, payload)
                    continue
                if isinstance(response_id, int):
                    future = session.pending.get(response_id)
                    if future is not None and not future.done():
                        future.set_result(payload)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except Exception as exc:
            for future in list(session.pending.values()):
                if not future.done():
                    future.set_exception(LspError(f"language server protocol error: {exc}"))
        finally:
            for future in list(session.pending.values()):
                if not future.done():
                    future.set_exception(LspError("language server connection closed"))

    async def _reply_to_server_request(self, session: _LspSession, payload: dict[str, Any]) -> None:
        method = str(payload.get("method") or "")
        params = payload.get("params")
        if method == "workspace/configuration":
            items = params.get("items", []) if isinstance(params, dict) else []
            result: Any = [None for _ in items] if isinstance(items, list) else []
        elif method == "workspace/workspaceFolders":
            result = [{"uri": session.root.as_uri(), "name": session.root.name}]
        elif method == "workspace/applyEdit":
            result = {
                "applied": False,
                "failureReason": "CPTR rejects server-initiated workspace edits",
            }
        else:
            # Dynamic registration, progress creation, message requests, and
            # unknown extension requests receive a deterministic null response
            # instead of being left pending indefinitely.
            result = None
        await self._send(
            session,
            {"jsonrpc": "2.0", "id": payload.get("id"), "result": result},
        )

    async def _stderr_loop(self, session: _LspSession) -> None:
        stderr = session.process.stderr
        if stderr is None:
            return
        while True:
            chunk = await stderr.read(4096)
            if not chunk:
                return
            session.stderr_tail.extend(chunk)
            if len(session.stderr_tail) > MAX_LSP_STDERR_BYTES:
                del session.stderr_tail[:-MAX_LSP_STDERR_BYTES]

    async def shutdown_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(
            *(self._terminate(session) for session in sessions), return_exceptions=True
        )

    async def _terminate(self, session: _LspSession) -> None:
        process = session.process
        if process.returncode is None:
            if os.name == "nt":
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(process.pid), "/T"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                if os.name == "nt":
                    await asyncio.to_thread(
                        subprocess.run,
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        process.kill()
                await process.wait()
        for task in (session.reader_task, session.stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


lsp_manager = LspManager()
