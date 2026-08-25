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


if __name__ == "__main__":
    unittest.main()
