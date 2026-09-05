import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.routers.coding import (
    BatchFileRequest,
    ReadManyRequest,
    ReadRequest,
    SearchRequest,
    read_many_workspace_files,
    read_workspace_file,
    search_workspace_files,
)
from cptr.services.execution_manager import CommandSessionRegistry, command_session_registry
from cptr.services.live_events import LiveEventHub, LiveEventStore, command_target_key
from cptr.utils.runtime import _list_tree_entries, _read_text_file, _read_text_files
from cptr.utils.tools import (
    _STOP_SESSION_WRITER,
    _command_event_writer,
    cancel_owned_command_sessions,
    command_sessions,
    run_command,
    start_command_session_manager,
)


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

    def test_bounded_runtime_read_rejects_oversized_file_before_content_load(self):
        with tempfile.TemporaryDirectory() as root_value:
            path = Path(root_value) / "large.txt"
            path.write_text("x" * 128, encoding="utf-8")
            with self.assertRaises(Exception) as caught:
                _read_text_file(str(path), 64)
        self.assertIn("File too large", str(caught.exception))

    def test_bounded_runtime_batch_preserves_order_in_one_operation(self):
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            paths = []
            for index in range(4):
                path = root / f"file-{index}.txt"
                path.write_text(f"value-{index}\n", encoding="utf-8")
                paths.append(str(path))
            result = _read_text_files(paths, 1_024)
        self.assertEqual(
            [item["name"] for item in result["files"]],
            [f"file-{index}.txt" for index in range(4)],
        )


class DirectCodingIoPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_read_uses_one_bounded_runtime_operation(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-perf", user_id="user-1")
        body = ReadRequest(path="file.py")
        bounded_read = AsyncMock(
            return_value={
                "path": "/tmp/cptr-perf/file.py",
                "name": "file.py",
                "size": 12,
                "binary": False,
                "content": "value = 1\n",
                "language": "python",
            }
        )
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.Runtime.read_text_file", bounded_read),
            patch(
                "cptr.routers.coding.Runtime.stat",
                new=AsyncMock(side_effect=AssertionError("redundant stat used")),
            ),
        ):
            result = await read_workspace_file(request, "ws-1", body)

        bounded_read.assert_awaited_once_with(request, "/tmp/cptr-perf/file.py", 500_000)
        self.assertEqual(result["content"], "value = 1\n")
        self.assertEqual(result["size"], 12)

    async def test_read_many_uses_one_bounded_batch_runtime_operation_and_preserves_order(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/cptr-perf", user_id="user-1")
        body = ReadManyRequest(
            files=[BatchFileRequest(path=f"file-{index}.txt") for index in range(4)],
            max_chars=10_000,
        )
        batch_read = AsyncMock(
            return_value={
                "files": [
                    {
                        "path": f"/tmp/cptr-perf/file-{index}.txt",
                        "name": f"file-{index}.txt",
                        "size": 16,
                        "binary": False,
                        "content": f"file-{index}.txt\n",
                        "language": "text",
                    }
                    for index in range(4)
                ]
            }
        )
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch("cptr.routers.coding.Runtime.read_text_files", batch_read),
        ):
            result = await read_many_workspace_files(request, "ws-1", body)

        batch_read.assert_awaited_once_with(
            request,
            [f"/tmp/cptr-perf/file-{index}.txt" for index in range(4)],
            500_000,
        )
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


class CommandSessionAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_spawn_releases_reserved_capacity(self):
        identity = SimpleNamespace(is_pam=False, app_user_id="pool-failed-spawn")
        request = SimpleNamespace()
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context",
                    new=AsyncMock(return_value=identity),
                ),
                patch(
                    "cptr.utils.tools.asyncio.create_subprocess_shell",
                    new=AsyncMock(side_effect=RuntimeError("spawn failed")),
                ),
            ):
                result = await run_command(
                    "ignored",
                    ".",
                    0,
                    __context__={
                        "workspace": workspace_root,
                        "workspace_id": "ws-pool",
                        "request": request,
                        "user_id": "pool-failed-spawn",
                    },
                    __use_pty=False,
                )

        self.assertEqual(result, "Error: spawn failed")
        self.assertEqual(command_session_registry.launch_reservation_count("pool-failed-spawn"), 0)

    async def test_cancelled_spawn_releases_reserved_capacity(self):
        identity = SimpleNamespace(is_pam=False, app_user_id="pool-cancelled-spawn")
        request = SimpleNamespace()
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context",
                    new=AsyncMock(return_value=identity),
                ),
                patch(
                    "cptr.utils.tools.asyncio.create_subprocess_shell",
                    new=AsyncMock(side_effect=asyncio.CancelledError()),
                ),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await run_command(
                        "ignored",
                        ".",
                        0,
                        __context__={
                            "workspace": workspace_root,
                            "workspace_id": "ws-pool",
                            "request": request,
                            "user_id": "pool-cancelled-spawn",
                        },
                        __use_pty=False,
                    )

        self.assertEqual(
            command_session_registry.launch_reservation_count("pool-cancelled-spawn"), 0
        )

    async def test_unexpected_setup_failure_releases_reservation_and_kills_spawned_process(self):
        identity = SimpleNamespace(is_pam=False, app_user_id="pool-setup-failure")
        request = SimpleNamespace()
        fake_proc = SimpleNamespace(pid=12345, returncode=None)
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context",
                    new=AsyncMock(return_value=identity),
                ),
                patch(
                    "cptr.utils.tools.asyncio.create_subprocess_shell",
                    new=AsyncMock(return_value=fake_proc),
                ),
                patch(
                    "cptr.utils.tools.Runtime.write_file",
                    new=AsyncMock(side_effect=RuntimeError("unexpected setup failure")),
                ),
                patch("cptr.utils.tools._kill_process_group") as kill,
            ):
                result = await run_command(
                    "ignored",
                    ".",
                    0,
                    __context__={
                        "workspace": workspace_root,
                        "workspace_id": "ws-pool",
                        "request": request,
                        "user_id": "pool-setup-failure",
                    },
                    __use_pty=False,
                )

        self.assertEqual(result, "Error: unexpected setup failure")
        self.assertEqual(command_session_registry.launch_reservation_count("pool-setup-failure"), 0)
        kill.assert_called_once_with(12345, force=True)

    async def test_cancelled_post_registration_setup_stops_unreturned_process(self):
        identity = SimpleNamespace(is_pam=False, app_user_id="pool-post-register-cancel")
        request = SimpleNamespace()
        fake_stdin = SimpleNamespace(write=lambda _data: None, drain=lambda: None)
        fake_proc = SimpleNamespace(
            pid=12345,
            stdin=fake_stdin,
            stdout=None,
            stderr=None,
            returncode=None,
        )
        blocker = asyncio.Event()
        process_stopped = asyncio.Event()

        async def blocked_wait(*_args, **_kwargs):
            await process_stopped.wait()
            fake_proc.returncode = -9
            return -9

        async def blocked_capture(*_args, **_kwargs):
            await blocker.wait()

        def kill_process(pid: int, force: bool = False):
            self.assertEqual(pid, 12345)
            if force:
                process_stopped.set()

        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context",
                    new=AsyncMock(return_value=identity),
                ),
                patch(
                    "cptr.utils.tools.asyncio.create_subprocess_shell",
                    new=AsyncMock(return_value=fake_proc),
                ),
                patch("cptr.utils.tools.Runtime.write_file", new=AsyncMock(return_value={})),
                patch("cptr.utils.tools._wait_for_command_process", side_effect=blocked_wait),
                patch(
                    "cptr.utils.tools.stream_command_session_output", side_effect=blocked_capture
                ),
                patch("cptr.utils.tools._command_log_writer", side_effect=blocked_capture),
                patch(
                    "cptr.utils.tools.drain_command_session_input",
                    new=AsyncMock(side_effect=asyncio.CancelledError()),
                ),
                patch("cptr.utils.tools._kill_process_group", side_effect=kill_process) as kill,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await run_command(
                        "ignored",
                        ".",
                        0,
                        __context__={
                            "workspace": workspace_root,
                            "request": request,
                            "user_id": "pool-post-register-cancel",
                        },
                        __use_pty=False,
                        __stdin="x",
                    )

                owned = [
                    (command_id, session)
                    for command_id, session in command_sessions.items()
                    if session.get("user_id") == "pool-post-register-cancel"
                ]
                self.assertEqual(len(owned), 1)
                command_id, session = owned[0]
                self.assertIsInstance(session.get("process_wait_task"), asyncio.Task)
                self.assertIsInstance(session.get("log_task"), asyncio.Task)
                self.assertIsInstance(session.get("log_writer_task"), asyncio.Task)
                kill.assert_called_once_with(12345, force=True)
                self.assertEqual(
                    command_session_registry.active_count("pool-post-register-cancel"), 0
                )
                tasks = [
                    task
                    for task in (
                        session.get("process_wait_task"),
                        session.get("log_task"),
                        session.get("log_writer_task"),
                    )
                    if isinstance(task, asyncio.Task)
                ]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                command_sessions.pop(command_id, None)

    async def test_concurrent_launches_never_oversubscribe_five_slot_pool(self):
        identity = SimpleNamespace(is_pam=False, app_user_id="pool-user")
        request = SimpleNamespace()
        message_id = "pool-admission-race"
        with tempfile.TemporaryDirectory() as workspace_root:
            with (
                patch(
                    "cptr.utils.tools.identity_for_context",
                    new=AsyncMock(return_value=identity),
                ),
                patch("cptr.utils.tools.Runtime.write_file", new=AsyncMock(return_value={})),
            ):
                command = f'{sys.executable} -c "import time; time.sleep(30)"'
                try:
                    results = await asyncio.gather(
                        *[
                            run_command(
                                command,
                                ".",
                                0,
                                __context__={
                                    "workspace": workspace_root,
                                    "workspace_id": "ws-pool",
                                    "request": request,
                                    "user_id": "pool-user",
                                    "message_id": message_id,
                                },
                                __use_pty=False,
                            )
                            for _ in range(6)
                        ]
                    )
                    accepted = [result for result in results if result.startswith("Task ")]
                    rejected = [result for result in results if result.startswith("Error:")]
                    self.assertEqual(len(accepted), 5)
                    self.assertEqual(len(rejected), 1)
                    self.assertIn("too many running command sessions (5/5)", rejected[0])
                finally:
                    await cancel_owned_command_sessions(message_id, timeout=2.0)
                    for command_id, session in list(command_sessions.items()):
                        if session.get("message_id") == message_id:
                            command_sessions.pop(command_id, None)


class CommandSessionRetentionTests(unittest.TestCase):
    def test_launch_reservation_context_releases_capacity_on_arbitrary_exception(self):
        registry = CommandSessionRegistry()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with registry.reserve_launch("user-1", 5) as token:
                self.assertIsNotNone(token)
                self.assertEqual(registry.launch_reservation_count("user-1"), 1)
                raise RuntimeError("boom")

        self.assertEqual(registry.launch_reservation_count("user-1"), 0)

    def test_new_lifespan_clears_stale_launch_reservations(self):
        token = command_session_registry.try_reserve_launch("stale-lifespan-user", 5)
        self.assertIsNotNone(token)
        self.assertEqual(
            command_session_registry.launch_reservation_count("stale-lifespan-user"), 1
        )

        start_command_session_manager()

        self.assertEqual(
            command_session_registry.launch_reservation_count("stale-lifespan-user"), 0
        )

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

    def test_registry_uses_completed_process_watcher_when_process_handle_is_stale(self):
        registry = CommandSessionRegistry()
        registry.register(
            "watched",
            {
                "done": False,
                "user_id": "user-1",
                "created_at": 1.0,
                "proc": SimpleNamespace(returncode=None, poll=lambda: None),
                "process_wait_task": SimpleNamespace(done=lambda: True, result=lambda: 0),
                "output": bytearray(),
            },
        )

        self.assertEqual(registry.active_count("user-1"), 0)
        self.assertTrue(registry.sessions["watched"]["done"])
        self.assertEqual(registry.sessions["watched"]["exit_code"], 0)

    def test_registry_reconciles_exited_process_before_counting_active_slots(self):
        registry = CommandSessionRegistry()
        registry.register(
            "stale",
            {
                "done": False,
                "user_id": "user-1",
                "created_at": 1.0,
                "proc": SimpleNamespace(returncode=0),
                "output": bytearray(),
            },
        )
        registry.register(
            "running",
            {
                "done": False,
                "user_id": "user-1",
                "created_at": 2.0,
                "proc": SimpleNamespace(returncode=None, poll=lambda: None),
                "output": bytearray(),
            },
        )

        self.assertEqual(registry.active_count("user-1"), 1)
        self.assertTrue(registry.sessions["stale"]["done"])
        self.assertEqual(registry.sessions["stale"]["exit_code"], 0)
        self.assertIn("completed_at", registry.sessions["stale"])

    def test_registry_frees_slot_when_process_exited_but_capture_is_still_draining(self):
        registry = CommandSessionRegistry()
        registry.register(
            "draining",
            {
                "done": False,
                "user_id": "user-1",
                "created_at": 1.0,
                "proc": SimpleNamespace(returncode=0),
                "log_task": SimpleNamespace(done=lambda: False),
                "output": bytearray(),
            },
        )

        self.assertEqual(registry.active_count("user-1"), 0)
        self.assertFalse(registry.sessions["draining"]["done"])

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
