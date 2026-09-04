import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from cptr.services.live_events import (
    LiveEventHub,
    LiveEventStore,
    TerminalStreamSanitizer,
    publish_command_event,
    sanitize_terminal_text,
)


class _LoopRecordingPersistentStore(LiveEventStore):
    def __init__(self):
        super().__init__(persistent=True)
        self.sequences = {}

    async def _persist_batch(self, batch):
        for pending in batch:
            sequence = self.sequences.get(pending.target_key, 0) + 1
            self.sequences[pending.target_key] = sequence
            envelope = self._envelope(
                event_id=f"event-{sequence}",
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
            if not pending.future.done():
                pending.future.set_result(envelope)


class LiveEventCrossLoopTests(unittest.TestCase):
    def test_persistent_store_rebinds_cleanly_across_event_loop_lifespans(self):
        store = _LoopRecordingPersistentStore()
        hub = LiveEventHub(store=store)
        subscriber_lock_ids = []

        async def cycle():
            await hub.start()
            subscriber_lock_ids.append(id(hub._subscriber_lock))
            try:
                return await hub.publish(
                    user_id="user-1",
                    target_key="task:task-1",
                    task_id="task-1",
                    event_type="task.started",
                    payload={"status": "RUNNING"},
                )
            finally:
                await hub.close()

        first = asyncio.run(cycle())
        second = asyncio.run(cycle())

        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)
        self.assertNotEqual(subscriber_lock_ids[0], subscriber_lock_ids[1])


class LiveEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_have_bounded_envelope_and_monotonic_target_sequence(self):
        store = LiveEventStore(max_payload_chars=120)
        hub = LiveEventHub(store=store)

        first = await hub.publish(
            user_id="user-1",
            target_key="task:task-1",
            task_id="task-1",
            event_type="task.started",
            payload={"status": "RUNNING", "secret": "Bearer should-not-escape"},
        )
        second = await hub.publish(
            user_id="user-1",
            target_key="task:task-1",
            task_id="task-1",
            event_type="agent.output",
            payload={"text": "x" * 500},
        )

        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)
        self.assertEqual(first.task_id, "task-1")
        self.assertNotIn("Bearer", str(first.payload))
        self.assertLessEqual(len(str(second.payload)), 200)

    async def test_replay_returns_only_events_after_sequence_for_one_target(self):
        store = LiveEventStore()
        hub = LiveEventHub(store=store)
        for target in ("task:task-1", "task:task-2"):
            await hub.publish(
                user_id="user-1",
                target_key=target,
                task_id=target.split(":", 1)[1],
                event_type="task.started",
                payload={"status": "RUNNING"},
            )
        await hub.publish(
            user_id="user-1",
            target_key="task:task-1",
            task_id="task-1",
            event_type="task.completed",
            payload={"status": "COMPLETE"},
        )

        replay = await store.replay("task:task-1", after_sequence=1, limit=10)
        self.assertEqual([item.sequence for item in replay], [2])
        self.assertEqual(replay[0].event_type, "task.completed")

    async def test_terminal_output_is_redacted_and_control_sequences_are_removed(self):
        store = LiveEventStore(max_payload_chars=8_000)
        event = await store.append(
            user_id="user-1",
            target_key="task:task-1",
            task_id="task-1",
            event_type="terminal.chunk",
            payload={
                "text": "token=super-secret-value \x1b]52;c;clipboard\x07 \x1b[31mred\x1b[0m /home/user/private.txt",
                "stream": "stdout",
            },
        )
        payload = event.to_dict()
        text = payload["payload"]["text"]
        self.assertEqual(payload["version"], 1)
        self.assertTrue(payload["redaction_applied"])
        self.assertNotIn("super-secret-value", text)
        self.assertNotIn("\\x1b", text)
        self.assertNotIn("/home/user/private.txt", text)
        self.assertIn("<workspace-path>", text)

    def test_terminal_stream_sanitizer_preserves_safe_sgr_across_chunk_boundaries(self):
        sanitizer = TerminalStreamSanitizer()
        first = sanitizer.feed("prefix \x1b[31")
        second = sanitizer.feed(";1mRED\x1b[0m suffix")
        self.assertEqual(first, "prefix ")
        self.assertEqual(second, "\x1b[31;1mRED\x1b[0m suffix")
        self.assertEqual(sanitizer.feed("", final=True), "")

    def test_terminal_stream_sanitizer_normalizes_progress_backspace_and_dangerous_controls(self):
        sanitizer = TerminalStreamSanitizer()
        text = sanitizer.feed("next\b!\rprogress\x1b]52;c;clipboard\x07\x1b[2Jdone", final=True)
        self.assertEqual(text, "nex!\nprogressdone")
        self.assertEqual(sanitize_terminal_text("\x1b[32mgreen\x1b[0m"), "\x1b[32mgreen\x1b[0m")

    async def test_command_completion_reconciles_matching_workbench_target_without_blocking_live_event(
        self,
    ):
        store = LiveEventStore()
        hub = LiveEventHub(store=store)
        reconcile = AsyncMock(return_value=1)
        with (
            patch("cptr.services.live_events.live_event_hub", hub),
            patch(
                "cptr.services.live_events.workbench_session_store.reconcile_command_terminal",
                new=reconcile,
            ),
        ):
            event = await publish_command_event(
                user_id="user-1",
                workspace_id="ws-1",
                command_id="cmd-1",
                event_type="command.completed",
                payload={"status": "COMPLETE", "exit_code": 0},
            )

        self.assertEqual(event.event_type, "command.completed")
        reconcile.assert_awaited_once_with(
            owner_id="user-1",
            workspace_id="ws-1",
            command_id="cmd-1",
            status="COMPLETE",
            exit_code=0,
        )

    async def test_snapshot_replays_only_events_after_cursor(self):
        store = LiveEventStore()
        await store.append(
            user_id="user-1",
            target_key="task:task-1",
            task_id="task-1",
            event_type="command.started",
            payload={"command_id": "cmd-1", "summary": "running"},
        )
        await store.append(
            user_id="user-1",
            target_key="task:task-1",
            task_id="task-1",
            event_type="terminal.chunk",
            payload={"command_id": "cmd-1", "text": "safe output"},
        )
        snapshot = await store.snapshot("task:task-1", after_sequence=1)
        self.assertEqual(snapshot["after_sequence"], 1)
        self.assertEqual(snapshot["last_sequence"], 2)
        self.assertEqual([event["sequence"] for event in snapshot["events"]], [2])
        self.assertEqual(snapshot["events"][0]["target"], {"type": "task", "id": "task-1"})


if __name__ == "__main__":
    unittest.main()
