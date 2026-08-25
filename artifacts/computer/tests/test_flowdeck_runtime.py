import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.flowdeck.durable import DurableFlowDeck, RunStatus
from cptr.flowdeck.runtime import (
    ManagedRuntimeService,
    RuntimeContractError,
    RuntimeRequest,
    discover_start_command,
)
from cptr.models.base import Base
from cptr.models.workspaces import Workspace


class ManagedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "main.py").write_text(
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "import os\n"
            "class H(BaseHTTPRequestHandler):\n"
            " def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
            "HTTPServer(('127.0.0.1', int(os.environ['PORT'])), H).serve_forever()\n",
            encoding="utf-8",
        )
        self.db_file = tempfile.NamedTemporaryFile(delete=False)
        self.db_file.close()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_file.name}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.store = DurableFlowDeck(async_sessionmaker(self.engine, expire_on_commit=False))
        async with self.store.session_factory() as session:
            session.add(Workspace(user_id="runtime-user", path=str(self.root), name="runtime", data={}, created_at=1))
            await session.commit()
        self.service = ManagedRuntimeService()

    async def asyncTearDown(self):
        for process in list(self.service._processes.values()):
            if process.process.returncode is None:
                process.process.kill()
                await process.process.wait()
        await self.engine.dispose()
        os.unlink(self.db_file.name)
        self.temp.cleanup()

    async def test_discovery_health_logs_durable_and_stop_is_authoritative(self):
        self.assertEqual(discover_start_command(self.root), ("python", "main.py"))
        result = await self.service.start(
            RuntimeRequest("runtime-test-1", str(self.root), "runtime-user"),
            store=self.store,
        )
        for _ in range(80):
            result = await self.service.status(result["run_id"], store=self.store)
            if result["state"] == "running":
                break
            await asyncio.sleep(0.05)
        self.assertEqual(result["health"], "healthy")
        self.assertIn("port", result)
        stopped = await self.service.stop(result["run_id"], store=self.store)
        self.assertEqual(stopped["state"], "stopped")
        run = await self.store.get_run(result["run_id"])
        self.assertEqual(run.status, RunStatus.CANCELLED.value)

    async def test_unsupported_project_and_unknown_recovery_fail_closed(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(RuntimeContractError):
            await self.service.start(RuntimeRequest("runtime-test-2", str(empty), "runtime-user"), store=self.store)
        run, _ = await self.store.create_run(
            request_key="runtime-lost", owner="runtime-user", workspace=str(self.root)
        )
        unknown = await self.service.status(run.id, store=self.store)
        self.assertEqual(unknown["state"], "unknown")
        self.assertTrue(unknown["evidence"]["authoritative"])