import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.routers.coding import (
    BatchFileRequest,
    ReadManyRequest,
    SearchRequest,
    read_many_workspace_files,
    search_workspace_files,
)
from cptr.services.execution_manager import CommandSessionRegistry
from cptr.services.live_events import LiveEventHub, LiveEventStore, command_target_key
from cptr.utils.runtime import _list_tree_entries
from cptr.utils.tools import _STOP_SESSION_WRITER, _command_event_writer, command_sessions


class FilesystemPerformanceContractTests(unittest.TestCase):
    def test_nonrecursive_tree_listing_never_walks_descendants(self):
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            (root / "src" / "deep").mkdir(parents=True)
            (root / "src" / "deep" / "hidden.py").write_text("deep = True\n", encoding="utf-8")
            (root / "top.py").write_text("top = True\n", encoding="utf-8")

            # The historical implementation called rglob() on every immediate
            # directory merely to calculate a recursive file count.
            with patch.object(Path, "rglob", side_effect=AssertionError("recursive scan used")):
                result = _list_tree_entries(str(root), False, 0, 100)

        paths = {entry["path"] for entry in result["entries"]}
        self.assertEqual(paths, {"src", "top.py"})
        self.assertFalse(result["truncated"])
        self.assertTrue(result["total_exact"])

    def test_recursive_tree_listing_stops_at_page_boundary(self):
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            for index in range(40):
                (root / f"file-{index:02d}.txt").write_text(str(index), encoding="utf-8")
            first = _list_tree_entries(str(root), True, 0, 7)
            second = _list_tree_entries(str(root), True, int(first["next_offset"]), 7)

        self.assertEqual(len(first["entries"]), 7)
        self.assertTrue(first["truncated"])
        self.assertFalse(first["total_exact"])
        self.assertEqual(len(second["entries"]), 7)
        self.assertNotEqual(first["entries"][0]["path"], second["entries"][0]["path"])


class DirectCodingIoPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_many_uses_bounded_concurrent_io_and_preserves_order(self):
        active = 0
        peak = 0

        async def read_file(_request, path):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            name = Path(path).name
            return {"binary": False, "content": f"{name}\n"}

        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-perf", user_id="user-1")
        body = ReadManyRequest(
            files=[BatchFileRequest(path=f"file-{index}.txt") for index in range(4)],
            max_chars=10_000,
        )
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.Runtime.read_file", side_effect=read_file),
        ):
            result = await read_many_workspace_files(request, "ws-1", body)

        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assertEqual(
            [item["path"] for item in result["files"]],
            [f"file-{index}.txt" for index in range(4)],
        )

    async def test_search_context_reads_each_source_only_once(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-perf", user_id="user-1")
        body = SearchRequest(query="needle", path=".", context_lines=1, max_results=10)
        raw_matches = [
            "same.py:2:first needle",
            "same.py:4:second needle",
            "other.py:1:third needle",
        ]
        read = AsyncMock(
            side_effect=[
                {"binary": False, "content": "a\nb\nc\nd\ne\n"},
                {"binary": False, "content": "x\ny\n"},
            ]
        )
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.search_files", new=AsyncMock(return_value=raw_matches)),
            patch("cptr.routers.coding.Runtime.read_file", read),
        ):
            result = await search_workspace_files(request, "ws-1", body)

        self.assertEqual(read.await_count, 2)
        self.assertEqual(len(result["matches"]), 3)
        self.assertTrue(all("context" in item for item in result["matches"]))


class TerminalCoalescingPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_pty_read_is_split_without_losing_live_output(self):
        hub = LiveEventHub(store=LiveEventStore())
        command_id = "perf-large-chunk"
        session = {
            "event_queue": asyncio.Queue(maxsize=64),
            "live_target": {
                "target_type": "command",
                "target_id": command_id,
                "workspace_id": "ws-1",
            },
            "user_id": "user-1",
            "message_id": None,
            "terminal_events_published": 0,
        }
        command_sessions[command_id] = session
        try:
            with patch("cptr.services.live_events.live_event_hub", hub):
                writer = asyncio.create_task(_command_event_writer(command_id))
                payload = b"x" * 40_000
                await session["event_queue"].put(("terminal.bytes", payload))
                await session["event_queue"].put(_STOP_SESSION_WRITER)
                await writer
            events = await hub.store.replay(command_target_key("ws-1", command_id))
        finally:
            command_sessions.pop(command_id, None)

        terminal_events = [event for event in events if event.event_type == "terminal.chunk"]
        combined = "".join(str(event.payload.get("text") or "") for event in terminal_events)
        self.assertEqual(combined, "x" * 40_000)
        self.assertGreater(len(terminal_events), 1)
        self.assertTrue(
            all(len(str(event.payload.get("text") or "")) <= 8_192 for event in terminal_events)
        )


class CommandSessionRetentionTests(unittest.TestCase):
    def test_registry_reaps_expired_completed_sessions(self):
        registry = CommandSessionRegistry()
        registry.register(
            "expired",
            {"done": True, "created_at": 1.0, "completed_at": 10.0, "output": bytearray(b"x")},
        )
        registry.register(
            "active",
            {"done": False, "created_at": 1.0, "output": bytearray(b"y")},
        )
        with patch("cptr.services.execution_manager.COMMAND_SESSION_TTL_SECONDS", 30):
            removed = registry.reap(now=100.0)

        self.assertEqual(removed, ["expired"])
        self.assertIn("active", registry.sessions)
        self.assertEqual(registry.stats()["total_reaped"], 1)

    def test_registry_enforces_hard_completed_retention_cap(self):
        registry = CommandSessionRegistry()
        for index in range(5):
            registry.register(
                str(index),
                {
                    "done": True,
                    "created_at": float(index),
                    "completed_at": float(index + 1),
                    "output": bytearray(),
                },
            )
        with (
            patch("cptr.services.execution_manager.COMMAND_SESSION_TTL_SECONDS", 10_000),
            patch("cptr.services.execution_manager.COMMAND_SESSION_MAX_RETAINED", 2),
        ):
            registry.reap(now=10.0)

        self.assertEqual(set(registry.sessions), {"3", "4"})
        self.assertEqual(registry.stats()["completed_retained"], 2)


class _RecordingPersistentStore(LiveEventStore):
    def __init__(self):
        super().__init__(persistent=True)
        self.batch_sizes = []
        self.sequences = {}

    async def _persist_batch(self, batch):
        self.batch_sizes.append(len(batch))
        for pending in batch:
            sequence = self.sequences.get(pending.target_key, 0) + 1
            self.sequences[pending.target_key] = sequence
            envelope = self._envelope(
                event_id=f"event-{pending.target_key}-{sequence}",
                sequence=sequence,
                created_at=pending.created_at,
                user_id=pending.user_id,
                target_key=pending.target_key,
                task_id=pending.task_id,
                monitor_id=pending.monitor_id,
                worker_task_id=pending.worker_task_id,
                event_type=pending.event_type,
                payload=pending.payload,
            )
            self._written_events += 1
            if not pending.future.done():
                pending.future.set_result(envelope)
        self._write_batches += 1


class LiveEventBatchingPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_durable_events_share_write_batches(self):
        store = _RecordingPersistentStore()
        started = time.perf_counter()
        try:
            events = await asyncio.gather(
                *(
                    store.append(
                        user_id="user-1",
                        target_key=f"task:{index % 4}",
                        task_id=f"task-{index % 4}",
                        event_type="terminal.chunk",
                        payload={"text": f"line-{index}"},
                    )
                    for index in range(64)
                )
            )
        finally:
            await store.close()

        self.assertEqual(len(events), 64)
        self.assertEqual(sum(store.batch_sizes), 64)
        self.assertTrue(any(size > 1 for size in store.batch_sizes))
        self.assertLess(len(store.batch_sizes), 64)
        # Generous guardrail: catches accidental sleeps/serial I/O, not machine speed.
        self.assertLess(time.perf_counter() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
