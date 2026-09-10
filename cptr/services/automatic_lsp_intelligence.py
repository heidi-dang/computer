"""Transparent, latency-bounded LSP intelligence for direct coding operations.

This layer intentionally sits behind the normal CPTR coding API. Callers do
not choose executables, start sessions, or issue raw LSP requests. CPTR maps a
source file to an administrator-controlled language server, keeps the session
warm per workspace/root, synchronizes document text, and returns bounded
language intelligence when it is available inside a small latency budget.

LSP is advisory here: a missing, warming, crashed, or slow server must never
turn an otherwise-valid filesystem read/write into a failure.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cptr.env import (
    AUTOMATIC_LSP_ENABLED,
    AUTOMATIC_LSP_IDLE_TTL_SECONDS,
    AUTOMATIC_LSP_MAX_DIAGNOSTICS,
    AUTOMATIC_LSP_MAX_SYMBOLS,
    AUTOMATIC_LSP_REQUEST_TIMEOUT_MS,
    AUTOMATIC_LSP_STARTUP_WAIT_MS,
)
from cptr.services.lsp_manager import LspError, LspManager, lsp_manager
from cptr.utils.identity import ExecutionIdentity, env_for, preexec_for


_LANGUAGE_BY_SUFFIX: dict[str, tuple[str, str]] = {
    ".py": ("pyright", "python"),
    ".pyi": ("pyright", "python"),
    ".ts": ("typescript", "typescript"),
    ".tsx": ("typescript", "typescriptreact"),
    ".js": ("typescript", "javascript"),
    ".jsx": ("typescript", "javascriptreact"),
    ".mjs": ("typescript", "javascript"),
    ".cjs": ("typescript", "javascript"),
    ".mts": ("typescript", "typescript"),
    ".cts": ("typescript", "typescript"),
    ".go": ("gopls", "go"),
    ".rs": ("rust-analyzer", "rust"),
    ".c": ("clangd", "c"),
    ".h": ("clangd", "c"),
    ".cc": ("clangd", "cpp"),
    ".cpp": ("clangd", "cpp"),
    ".cxx": ("clangd", "cpp"),
    ".hh": ("clangd", "cpp"),
    ".hpp": ("clangd", "cpp"),
    ".hxx": ("clangd", "cpp"),
}

_DEFAULT_STARTUP_WAIT_SECONDS = 0.35
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 0.35
_DEFAULT_IDLE_TTL_SECONDS = 10 * 60.0
_DEFAULT_MAX_SYMBOLS = 80
_DEFAULT_MAX_DIAGNOSTICS = 80
_MAX_STRING_CHARS = 4_000
_MAX_NESTING = 8
_MAX_COLLECTION_ITEMS = 100


@dataclass
class _AutomaticSession:
    lsp_id: str
    server_id: str
    user_id: str
    workspace_id: str
    root: Path
    capabilities: dict[str, Any]
    document_versions: dict[str, int] = field(default_factory=dict)
    last_used: float = field(default_factory=time.monotonic)


class AutomaticLspIntelligenceService:
    def __init__(
        self,
        *,
        manager: LspManager | Any = lsp_manager,
        enabled: bool = True,
        startup_wait_seconds: float = _DEFAULT_STARTUP_WAIT_SECONDS,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        idle_ttl_seconds: float = _DEFAULT_IDLE_TTL_SECONDS,
        max_symbols: int = _DEFAULT_MAX_SYMBOLS,
        max_diagnostics: int = _DEFAULT_MAX_DIAGNOSTICS,
    ) -> None:
        self._manager = manager
        self._enabled = bool(enabled)
        self._startup_wait_seconds = max(0.0, min(float(startup_wait_seconds), 5.0))
        self._request_timeout_seconds = max(0.05, min(float(request_timeout_seconds), 5.0))
        self._idle_ttl_seconds = max(5.0, min(float(idle_ttl_seconds), 24 * 60 * 60.0))
        self._max_symbols = max(1, min(int(max_symbols), 500))
        self._max_diagnostics = max(1, min(int(max_diagnostics), 500))
        self._sessions: dict[tuple[str, str, str, str], _AutomaticSession] = {}
        self._start_tasks: dict[tuple[str, str, str, str], asyncio.Task[_AutomaticSession]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def language_for_path(path: Path) -> tuple[str, str] | None:
        return _LANGUAGE_BY_SUFFIX.get(path.suffix.lower())

    @staticmethod
    def _key(
        *, user_id: str, workspace_id: str, root: Path, server_id: str
    ) -> tuple[str, str, str, str]:
        return user_id, workspace_id, str(root.resolve()), server_id

    @staticmethod
    def _bounded_value(value: Any, *, depth: int = 0) -> Any:
        if depth >= _MAX_NESTING:
            return "[LSP_MAX_DEPTH]"
        if isinstance(value, str):
            return value if len(value) <= _MAX_STRING_CHARS else value[:_MAX_STRING_CHARS]
        if isinstance(value, list):
            return [
                AutomaticLspIntelligenceService._bounded_value(item, depth=depth + 1)
                for item in value[:_MAX_COLLECTION_ITEMS]
            ]
        if isinstance(value, dict):
            return {
                str(key)[:200]: AutomaticLspIntelligenceService._bounded_value(
                    item, depth=depth + 1
                )
                for key, item in list(value.items())[:_MAX_COLLECTION_ITEMS]
            }
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:_MAX_STRING_CHARS]

    async def _start_session(
        self,
        *,
        user_id: str,
        workspace_id: str,
        root: Path,
        server_id: str,
        identity: ExecutionIdentity,
    ) -> _AutomaticSession:
        process_env = env_for(identity, root, {"PAGER": "cat", "GIT_PAGER": "cat"})
        process_preexec = preexec_for(identity) if identity.is_pam else None
        started = await self._manager.start(
            server_id=server_id,
            root=root,
            user_id=user_id,
            env=process_env,
            preexec_fn=process_preexec,
        )
        capabilities = started.get("capabilities")
        return _AutomaticSession(
            lsp_id=str(started["lsp_id"]),
            server_id=server_id,
            user_id=user_id,
            workspace_id=workspace_id,
            root=root,
            capabilities=capabilities if isinstance(capabilities, dict) else {},
        )

    async def _reap_idle(self, now: float) -> None:
        stale: list[_AutomaticSession] = []
        async with self._lock:
            for key, session in list(self._sessions.items()):
                if now - session.last_used > self._idle_ttl_seconds:
                    stale.append(session)
                    self._sessions.pop(key, None)
        if stale:
            await asyncio.gather(
                *(
                    self._manager.stop(lsp_id=session.lsp_id, user_id=session.user_id)
                    for session in stale
                ),
                return_exceptions=True,
            )

    async def _session_for(
        self,
        *,
        user_id: str,
        workspace_id: str,
        root: Path,
        server_id: str,
        identity: ExecutionIdentity,
    ) -> tuple[_AutomaticSession | None, str | None]:
        root = root.resolve()
        now = time.monotonic()
        await self._reap_idle(now)
        key = self._key(user_id=user_id, workspace_id=workspace_id, root=root, server_id=server_id)
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                existing.last_used = now
                return existing, None
            task = self._start_tasks.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._start_session(
                        user_id=user_id,
                        workspace_id=workspace_id,
                        root=root,
                        server_id=server_id,
                        identity=identity,
                    ),
                    name=f"cptr-auto-lsp-start-{server_id}",
                )
                self._start_tasks[key] = task

        try:
            if self._startup_wait_seconds <= 0:
                if not task.done():
                    return None, None
                session = task.result()
            else:
                session = await asyncio.wait_for(
                    asyncio.shield(task), timeout=self._startup_wait_seconds
                )
        except asyncio.TimeoutError:
            return None, None
        except (LspError, OSError, RuntimeError) as exc:
            async with self._lock:
                if self._start_tasks.get(key) is task:
                    self._start_tasks.pop(key, None)
            return None, str(exc)[:500]
        except Exception as exc:
            async with self._lock:
                if self._start_tasks.get(key) is task:
                    self._start_tasks.pop(key, None)
            return None, f"automatic LSP startup failed: {type(exc).__name__}"[:500]

        async with self._lock:
            self._sessions[key] = session
            if self._start_tasks.get(key) is task:
                self._start_tasks.pop(key, None)
            session.last_used = time.monotonic()
        return session, None

    async def prefetch(
        self,
        *,
        user_id: str,
        workspace_id: str,
        root: Path,
        path: Path,
        identity: ExecutionIdentity,
    ) -> None:
        language = self.language_for_path(path)
        if not self._enabled or language is None:
            return
        server_id, _ = language
        key = self._key(user_id=user_id, workspace_id=workspace_id, root=root, server_id=server_id)
        async with self._lock:
            if key in self._sessions or key in self._start_tasks:
                return
            self._start_tasks[key] = asyncio.create_task(
                self._start_session(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    root=root.resolve(),
                    server_id=server_id,
                    identity=identity,
                ),
                name=f"cptr-auto-lsp-prefetch-{server_id}",
            )

    async def _sync_document(
        self,
        *,
        session: _AutomaticSession,
        path: Path,
        language_id: str,
        content: str,
    ) -> str:
        uri = path.resolve().as_uri()
        previous = session.document_versions.get(uri)
        version = (previous or 0) + 1
        if previous is None:
            method = "textDocument/didOpen"
            params: dict[str, Any] = {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": version,
                    "text": content,
                }
            }
        else:
            method = "textDocument/didChange"
            params = {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": content}],
            }
        await asyncio.wait_for(
            self._manager.notify(
                lsp_id=session.lsp_id,
                user_id=session.user_id,
                method=method,
                params=params,
            ),
            timeout=self._request_timeout_seconds,
        )
        session.document_versions[uri] = version
        session.last_used = time.monotonic()
        await asyncio.sleep(0)
        return uri

    async def _request(
        self,
        *,
        session: _AutomaticSession,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        response = await self._manager.request(
            lsp_id=session.lsp_id,
            user_id=session.user_id,
            method=method,
            params=params,
            timeout_seconds=self._request_timeout_seconds,
        )
        if not isinstance(response, dict):
            return None
        if response.get("error") is not None:
            return None
        return response.get("result")

    async def _enrich(
        self,
        *,
        user_id: str,
        workspace_id: str,
        root: Path,
        path: Path,
        content: str,
        identity: ExecutionIdentity,
        position: dict[str, int] | None,
    ) -> dict[str, Any] | None:
        language = self.language_for_path(path)
        if not self._enabled or language is None:
            return None
        server_id, language_id = language
        session, start_error = await self._session_for(
            user_id=user_id,
            workspace_id=workspace_id,
            root=root,
            server_id=server_id,
            identity=identity,
        )
        if session is None:
            if start_error:
                return {
                    "provider": "lsp",
                    "server_id": server_id,
                    "status": "unavailable",
                    "reason": start_error,
                }
            return {"provider": "lsp", "server_id": server_id, "status": "warming"}

        uri = path.resolve().as_uri()
        symbols: list[Any] = []
        diagnostics: list[Any] = []
        hover: Any = None
        truncated = False
        errors: list[str] = []
        try:
            uri = await self._sync_document(
                session=session,
                path=path,
                language_id=language_id,
                content=content,
            )
        except (asyncio.TimeoutError, LspError, OSError, RuntimeError) as exc:
            errors.append(str(exc)[:300])

        if not errors and session.capabilities.get("documentSymbolProvider"):
            try:
                raw_symbols = await self._request(
                    session=session,
                    method="textDocument/documentSymbol",
                    params={"textDocument": {"uri": uri}},
                )
                if isinstance(raw_symbols, list):
                    truncated = truncated or len(raw_symbols) > self._max_symbols
                    symbols = raw_symbols[: self._max_symbols]
            except (asyncio.TimeoutError, LspError, OSError, RuntimeError) as exc:
                errors.append(str(exc)[:300])

        if not errors and position is not None and session.capabilities.get("hoverProvider"):
            try:
                hover = await self._request(
                    session=session,
                    method="textDocument/hover",
                    params={"textDocument": {"uri": uri}, "position": position},
                )
            except (asyncio.TimeoutError, LspError, OSError, RuntimeError) as exc:
                errors.append(str(exc)[:300])

        try:
            raw_diagnostics = self._manager.latest_diagnostics(
                lsp_id=session.lsp_id,
                user_id=session.user_id,
                uri=uri,
            )
            if isinstance(raw_diagnostics, list):
                truncated = truncated or len(raw_diagnostics) > self._max_diagnostics
                diagnostics = raw_diagnostics[: self._max_diagnostics]
        except (LspError, OSError, RuntimeError):
            diagnostics = []

        result: dict[str, Any] = {
            "provider": "lsp",
            "server_id": server_id,
            "status": "degraded" if errors else "ok",
            "symbols": self._bounded_value(symbols),
            "diagnostics": self._bounded_value(diagnostics),
            "truncated": truncated,
        }
        if hover is not None:
            result["hover"] = self._bounded_value(hover)
        if errors:
            result["reason"] = errors[0]
        return result

    async def enrich_read(
        self,
        *,
        user_id: str,
        workspace_id: str,
        root: Path,
        path: Path,
        content: str,
        identity: ExecutionIdentity,
        position: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        return await self._enrich(
            user_id=user_id,
            workspace_id=workspace_id,
            root=root,
            path=path,
            content=content,
            identity=identity,
            position=position,
        )

    async def enrich_after_write(
        self,
        *,
        user_id: str,
        workspace_id: str,
        root: Path,
        path: Path,
        content: str,
        identity: ExecutionIdentity,
    ) -> dict[str, Any] | None:
        return await self._enrich(
            user_id=user_id,
            workspace_id=workspace_id,
            root=root,
            path=path,
            content=content,
            identity=identity,
            position=None,
        )

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            tasks = list(self._start_tasks.values())
            self._sessions.clear()
            self._start_tasks.clear()
        completed: list[_AutomaticSession] = []
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            if task.done() and not task.cancelled():
                try:
                    completed.append(task.result())
                except Exception:
                    pass
        unique: dict[str, _AutomaticSession] = {
            session.lsp_id: session for session in [*sessions, *completed]
        }
        if unique:
            await asyncio.gather(
                *(
                    self._manager.stop(lsp_id=session.lsp_id, user_id=session.user_id)
                    for session in unique.values()
                ),
                return_exceptions=True,
            )


service = AutomaticLspIntelligenceService(
    enabled=AUTOMATIC_LSP_ENABLED,
    startup_wait_seconds=AUTOMATIC_LSP_STARTUP_WAIT_MS / 1000.0,
    request_timeout_seconds=AUTOMATIC_LSP_REQUEST_TIMEOUT_MS / 1000.0,
    idle_ttl_seconds=AUTOMATIC_LSP_IDLE_TTL_SECONDS,
    max_symbols=AUTOMATIC_LSP_MAX_SYMBOLS,
    max_diagnostics=AUTOMATIC_LSP_MAX_DIAGNOSTICS,
)
