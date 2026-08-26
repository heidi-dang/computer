import unittest

from cptr.services.live_events import LiveEventHub, LiveEventStore


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
