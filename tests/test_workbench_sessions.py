import asyncio
import os
import tempfile
import unittest
import uuid


class WorkbenchSessionStoreTests(unittest.TestCase):
    def test_owner_scoped_events_are_redacted_and_delete_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as data_dir:
            previous = os.environ.get("CPTR_DATA_DIR")
            os.environ["CPTR_DATA_DIR"] = data_dir
            try:
                asyncio.run(self._exercise_store())
            finally:
                if previous is None:
                    os.environ.pop("CPTR_DATA_DIR", None)
                else:
                    os.environ["CPTR_DATA_DIR"] = previous

    async def _exercise_store(self):
        # Import after CPTR_DATA_DIR setup so the storage layer uses this test DB.
        from cptr.models import User
        from cptr.services.workbench_sessions import WorkbenchSessionStore
        from cptr.utils.db import init_db

        await init_db()
        suffix = uuid.uuid4().hex
        owner_id = await User.create(f"workbench-owner-{suffix}", "hash", role="user", created_at=1)
        other_id = await User.create(f"workbench-other-{suffix}", "hash", role="user", created_at=1)
        store = WorkbenchSessionStore()

        session = await store.create(owner_id=owner_id, name="Audit /home/user/private-repo")
        self.assertTrue(session["session_id"].startswith("wbs_"))
        self.assertNotIn("/home/user", session["name"])

        event = await store.append_event(
            owner_id=owner_id,
            session_id=session["session_id"],
            source="plugin",
            actor="chatgpt_plugin",
            event_type="mcp.tool.completed",
            summary="Read /home/user/private-repo with token=secret-token-value",
            target_type="task",
            target_id="task_1",
            details={"authorization": "Bearer secret-token-value", "path": "/home/user/private-repo"},
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.sequence, 1)
        self.assertNotIn("/home/user", event.event["summary"])
        self.assertNotIn("secret-token-value", str(event.event))

        self.assertIsNone(await store.get(owner_id=other_id, session_id=session["session_id"]))
        self.assertIsNone(
            await store.events(owner_id=other_id, session_id=session["session_id"])
        )
        self.assertIsNone(
            await store.bind_target(
                owner_id=other_id,
                session_id=session["session_id"],
                target_type="task",
                target_id="task_1",
            )
        )

        bound = await store.bind_target(
            owner_id=owner_id,
            session_id=session["session_id"],
            target_type="task",
            target_id="task_1",
        )
        self.assertEqual(bound["active_target_id"], "task_1")

        delete_request = await store.request_delete(owner_id=owner_id, session_id=session["session_id"])
        self.assertIsNotNone(delete_request)
        assert delete_request is not None
        self.assertIsNone(
            await store.confirm_delete(owner_id=other_id, confirmation_id=delete_request["confirmation_id"])
        )
        deleted = await store.confirm_delete(
            owner_id=owner_id, confirmation_id=delete_request["confirmation_id"]
        )
        self.assertEqual(deleted["status"], "DELETED")
        self.assertIsNone(await store.get(owner_id=owner_id, session_id=session["session_id"]))


if __name__ == "__main__":
    unittest.main()
