import asyncio
import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from cptr.routers import events


class _FakeObserver:
    def __init__(self) -> None:
        self.daemon = False
        self.schedule_calls: list[tuple[str, bool]] = []
        self.started = False
        self.stopped = False
        self.joined = False
        self.watch = object()

    def schedule(self, handler, path: str, recursive: bool = False):
        self.schedule_calls.append((path, recursive))
        return self.watch

    def start(self) -> None:
        self.started = True

    def unschedule(self, watch) -> None:
        if watch is not self.watch:
            raise AssertionError("unexpected watch")

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout=None) -> None:
        self.joined = True


class SystemEventsRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        for path, entry in list(events._watch_registry.items()):
            for queue in list(entry.subscribers):
                await events._unsubscribe_from_path(path, queue)

        for queue in list(events._port_subscribers):
            await events._unsubscribe_port_events(queue)

        task = events._fs_dispatch_task
        if task is not None and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        events._fs_dispatch_task = None

    async def test_filesystem_watch_is_nonrecursive_and_cleanup_converges(self):
        observer = _FakeObserver()
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(events, "_create_observer", return_value=observer),
        ):
            resolved, queue = await events._subscribe_to_path(root)
            self.assertEqual(observer.schedule_calls, [(str(Path(root).resolve()), False)])
            self.assertTrue(observer.started)
            self.assertIn(resolved, events._watch_registry)

            await events._unsubscribe_from_path(resolved, queue)

        self.assertNotIn(resolved, events._watch_registry)
        self.assertTrue(observer.stopped)
        self.assertTrue(observer.joined)

    async def test_watch_limit_opens_circuit_without_killing_fs_task(self):
        ws = Mock()
        ws.send_json = AsyncMock()
        subscribe = AsyncMock(side_effect=OSError(errno.ENOSPC, "inotify watch limit reached"))

        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(events, "_ensure_fs_dispatcher", new=AsyncMock()),
                patch.object(events, "_subscribe_to_path", new=subscribe),
            ):
                task = asyncio.create_task(events._fs_watcher_loop(ws, root, {}))
                await asyncio.sleep(0.05)

                self.assertFalse(task.done())
                self.assertEqual(subscribe.await_count, 1)
                ws.send_json.assert_awaited_once()
                payload = ws.send_json.await_args.args[0]
                self.assertEqual(payload["type"], "fs_watch_error")
                self.assertEqual(payload["code"], "WATCH_LIMIT")
                self.assertFalse(payload["retryable"])

                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_multiple_clients_share_one_port_monitor(self):
        scan = AsyncMock(return_value=[])
        with (
            patch.object(events, "_scan_ports", new=scan),
            patch.object(events, "_PORT_SCAN_INTERVAL_SECONDS", 0.01),
        ):
            first = await events._subscribe_port_events()
            monitor = events._port_monitor_task
            second = await events._subscribe_port_events()

            self.assertIsNotNone(monitor)
            self.assertIs(events._port_monitor_task, monitor)
            self.assertEqual(len(events._port_subscribers), 2)

            await asyncio.sleep(0.035)
            self.assertGreaterEqual(scan.await_count, 1)

            await events._unsubscribe_port_events(first)
            self.assertIs(events._port_monitor_task, monitor)
            await events._unsubscribe_port_events(second)

        self.assertIsNone(events._port_monitor_task)
        self.assertEqual(events._port_snapshot, {})

    async def test_linux_port_scan_builds_inode_map_once_and_caches_process_name(self):
        lines = [
            "  0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 101 1\n",
            "  1: 0100007F:2328 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 102 1\n",
        ]

        async def fake_to_thread(func, *args, **kwargs):
            if func.__name__ == "_read_proc_net":
                return lines
            if func is events._linux_socket_pid_map:
                return {101: 4321, 102: 4321}
            raise AssertionError(f"unexpected to_thread target: {func}")

        process_name = AsyncMock(return_value="node")
        with (
            patch.object(events.asyncio, "to_thread", new=fake_to_thread),
            patch.object(events, "_get_process_name", new=process_name),
        ):
            result = await events._scan_ports_linux()

        self.assertEqual([item["port"] for item in result], [8080, 9000])
        self.assertTrue(all(item["pid"] == 4321 for item in result))
        self.assertTrue(all(item["process"] == "node" for item in result))
        process_name.assert_awaited_once_with(4321)


if __name__ == "__main__":
    unittest.main()
