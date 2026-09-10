import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cptr.services.automatic_lsp_intelligence import AutomaticLspIntelligenceService
from cptr.services.lsp_manager import LspError
from cptr.utils.identity import ExecutionIdentity


def _identity(home: str) -> ExecutionIdentity:
    return ExecutionIdentity(
        app_user_id="user-1",
        username="tester",
        uid=os.getuid() if hasattr(os, "getuid") else None,
        gid=os.getgid() if hasattr(os, "getgid") else None,
        groups=tuple(os.getgroups()) if hasattr(os, "getgroups") else (),
        home=home,
        shell=os.environ.get("SHELL", "/bin/sh"),
        is_pam=False,
    )


class _FakeManager:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.stops: list[str] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.start_gate: asyncio.Event | None = None
        self.start_error: Exception | None = None

    async def start(self, **kwargs: Any) -> dict[str, Any]:
        self.starts.append(kwargs)
        if self.start_error is not None:
            raise self.start_error
        if self.start_gate is not None:
            await self.start_gate.wait()
        return {
            "lsp_id": f"lsp-{len(self.starts)}",
            "server_id": kwargs["server_id"],
            "status": "RUNNING",
            "capabilities": {"documentSymbolProvider": True, "hoverProvider": True},
        }

    async def notify(self, *, method: str, params: Any, **_: Any) -> None:
        self.notifications.append((method, params))

    async def request(self, *, method: str, params: Any, **_: Any) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "textDocument/documentSymbol":
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": [
                    {"name": f"symbol-{index}", "kind": 12, "range": {}, "selectionRange": {}}
                    for index in range(20)
                ],
            }
        if method == "textDocument/hover":
            return {"jsonrpc": "2.0", "id": 2, "result": {"contents": "hover-info"}}
        return {"jsonrpc": "2.0", "id": 3, "result": None}

    def latest_diagnostics(self, **_: Any) -> list[dict[str, Any]]:
        return list(self.diagnostics)

    async def stop(self, *, lsp_id: str, **_: Any) -> dict[str, Any]:
        self.stops.append(lsp_id)
        return {"lsp_id": lsp_id, "status": "STOPPED"}


class AutomaticLspIntelligenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_supported_language_session_is_reused_and_document_versions_advance(self):
        manager = _FakeManager()
        service = AutomaticLspIntelligenceService(
            manager=manager,
            enabled=True,
            startup_wait_seconds=1.0,
            request_timeout_seconds=0.5,
            max_symbols=5,
            max_diagnostics=3,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample.py"
            source.write_text("value = 1\n", encoding="utf-8")
            identity = _identity(temp)

            first = await service.enrich_read(
                user_id="user-1",
                workspace_id="ws-1",
                root=root,
                path=source,
                content="value = 1\n",
                identity=identity,
                position={"line": 0, "character": 0},
            )
            second = await service.enrich_after_write(
                user_id="user-1",
                workspace_id="ws-1",
                root=root,
                path=source,
                content="value = 2\n",
                identity=identity,
            )

        self.assertEqual(len(manager.starts), 1)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["server_id"], "pyright")
        self.assertEqual(len(first["symbols"]), 5)
        self.assertEqual(first["hover"]["contents"], "hover-info")
        self.assertEqual(second["status"], "ok")
        methods = [method for method, _ in manager.notifications]
        self.assertEqual(methods[:2], ["textDocument/didOpen", "textDocument/didChange"])
        self.assertEqual(manager.notifications[0][1]["textDocument"]["version"], 1)
        self.assertEqual(manager.notifications[1][1]["textDocument"]["version"], 2)

    async def test_diagnostics_and_symbols_are_bounded(self):
        manager = _FakeManager()
        manager.diagnostics = [{"message": f"diagnostic-{index}"} for index in range(10)]
        service = AutomaticLspIntelligenceService(
            manager=manager,
            enabled=True,
            startup_wait_seconds=1.0,
            request_timeout_seconds=0.5,
            max_symbols=2,
            max_diagnostics=3,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample.ts"
            identity = _identity(temp)
            result = await service.enrich_read(
                user_id="user-1",
                workspace_id="ws-1",
                root=root,
                path=source,
                content="export const value = 1;\n",
                identity=identity,
            )

        self.assertEqual(result["server_id"], "typescript")
        self.assertEqual(len(result["symbols"]), 2)
        self.assertEqual(len(result["diagnostics"]), 3)
        self.assertTrue(result["truncated"])

    async def test_unsupported_files_do_not_start_language_server(self):
        manager = _FakeManager()
        service = AutomaticLspIntelligenceService(manager=manager, enabled=True)
        with tempfile.TemporaryDirectory() as temp:
            result = await service.enrich_read(
                user_id="user-1",
                workspace_id="ws-1",
                root=Path(temp),
                path=Path(temp, "README.md"),
                content="# hello\n",
                identity=_identity(temp),
            )
        self.assertIsNone(result)
        self.assertEqual(manager.starts, [])

    async def test_cold_start_timeout_returns_warming_without_cancelling_start(self):
        manager = _FakeManager()
        manager.start_gate = asyncio.Event()
        service = AutomaticLspIntelligenceService(
            manager=manager,
            enabled=True,
            startup_wait_seconds=0.01,
            request_timeout_seconds=0.5,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample.py"
            identity = _identity(temp)
            first = await service.enrich_read(
                user_id="user-1",
                workspace_id="ws-1",
                root=root,
                path=source,
                content="x = 1\n",
                identity=identity,
            )
            self.assertEqual(first["status"], "warming")
            manager.start_gate.set()
            for _ in range(50):
                await asyncio.sleep(0.01)
                second = await service.enrich_read(
                    user_id="user-1",
                    workspace_id="ws-1",
                    root=root,
                    path=source,
                    content="x = 1\n",
                    identity=identity,
                )
                if second["status"] == "ok":
                    break
            self.assertEqual(second["status"], "ok")
        self.assertEqual(len(manager.starts), 1)

    async def test_unavailable_language_server_degrades_without_raising(self):
        manager = _FakeManager()
        manager.start_error = LspError("language server is not installed: pyright")
        service = AutomaticLspIntelligenceService(
            manager=manager,
            enabled=True,
            startup_wait_seconds=1.0,
        )
        with tempfile.TemporaryDirectory() as temp:
            result = await service.enrich_read(
                user_id="user-1",
                workspace_id="ws-1",
                root=Path(temp),
                path=Path(temp, "sample.py"),
                content="x = 1\n",
                identity=_identity(temp),
            )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["server_id"], "pyright")
        self.assertIn("not installed", result["reason"])

    async def test_close_all_cancels_pending_start_tasks(self):
        manager = _FakeManager()
        manager.start_gate = asyncio.Event()
        service = AutomaticLspIntelligenceService(
            manager=manager,
            enabled=True,
            startup_wait_seconds=0.0,
        )
        with tempfile.TemporaryDirectory() as temp:
            await service.prefetch(
                user_id="user-1",
                workspace_id="ws-1",
                root=Path(temp),
                path=Path(temp, "sample.py"),
                identity=_identity(temp),
            )
            await asyncio.sleep(0)
            self.assertEqual(len(service._start_tasks), 1)
            task = next(iter(service._start_tasks.values()))
            await service.close_all()

        self.assertTrue(task.done())
        self.assertEqual(service._start_tasks, {})
        self.assertEqual(service._sessions, {})

    async def test_close_all_stops_cached_automatic_sessions(self):
        manager = _FakeManager()
        service = AutomaticLspIntelligenceService(
            manager=manager, enabled=True, startup_wait_seconds=1.0
        )
        with tempfile.TemporaryDirectory() as temp:
            await service.enrich_read(
                user_id="user-1",
                workspace_id="ws-1",
                root=Path(temp),
                path=Path(temp, "sample.py"),
                content="x = 1\n",
                identity=_identity(temp),
            )
        await service.close_all()
        self.assertEqual(manager.stops, ["lsp-1"])


if __name__ == "__main__":
    unittest.main()
