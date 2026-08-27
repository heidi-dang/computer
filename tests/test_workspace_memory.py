import asyncio
import os
import tempfile
import unittest
import uuid


class WorkspaceMemoryStoreTests(unittest.TestCase):
    def test_durable_owner_scoped_workspace_memory(self):
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
        from cptr.models import User, Workspace
        from cptr.services.workspace_memory import WorkspaceMemoryStore
        from cptr.utils.db import init_db

        await init_db()
        suffix = uuid.uuid4().hex
        owner_id = await User.create(f"memory-owner-{suffix}", "hash", role="user", created_at=1)
        other_id = await User.create(f"memory-other-{suffix}", "hash", role="user", created_at=1)
        workspace = await Workspace.upsert(owner_id, f"/tmp/cptr-memory-{suffix}", "Memory test", {})
        other_workspace = await Workspace.upsert(other_id, f"/tmp/cptr-other-{suffix}", "Other test", {})
        store = WorkspaceMemoryStore()

        first = await store.record_event(
            owner_id=owner_id,
            workspace_id=workspace.id,
            operation_id="mcp:read:one",
            kind="workspace.inspected",
            summary="Read /home/ubuntu/private-repo with token=not-a-real-token",
            tool_name="cptr_code_read_file",
            affected_paths=["src/app.py", "/home/ubuntu/private-repo/.env", "../escape.py"],
            details={
                "authorization": "Bearer not-a-real-token",
                "path": "/home/ubuntu/private-repo/src/app.py",
                "result_json": "should never be persisted",
                "count": 1,
            },
        )
        self.assertFalse(first["idempotent"])
        self.assertEqual(first["event"]["sequence"], 1)
        self.assertEqual(first["event"]["affected_paths"], ["src/app.py"])
        self.assertNotIn("private-repo", str(first))
        self.assertNotIn("not-a-real-token", str(first))
        self.assertNotIn("result_json", first["event"]["details"])

        duplicate = await store.record_event(
            owner_id=owner_id,
            workspace_id=workspace.id,
            operation_id="mcp:read:one",
            kind="workspace.inspected",
            summary="This retry must not create another event",
        )
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(duplicate["event"]["sequence"], 1)

        fact = await store.record_fact(
            owner_id=owner_id,
            workspace_id=workspace.id,
            category="convention",
            content="Run the focused test profile before changing the API contract.",
            pinned=True,
            paths=["src/app.py"],
            source_event_id=first["event"]["event_id"],
        )
        self.assertEqual(fact["status"], "ACTIVE")
        self.assertTrue(fact["pinned"])

        await store.record_event(
            owner_id=owner_id,
            workspace_id=workspace.id,
            operation_id="mcp:write:one",
            kind="workspace.changed",
            summary="ChatGPT completed cptr_code_edit_file.",
            affected_paths=["src/app.py"],
        )
        facts = await store.list_facts(owner_id=owner_id, workspace_id=workspace.id)
        self.assertEqual(facts["facts"][0]["status"], "STALE")

        owner_context = await store.get_context(owner_id=owner_id, workspace_id=workspace.id)
        self.assertEqual(owner_context["memory_cursor"], 2)
        self.assertEqual(owner_context["relevant_facts"], [])
        self.assertEqual(owner_context["workspace_stage"]["changed_paths"], ["src/app.py"])

        with self.assertRaises(LookupError):
            await store.get_context(owner_id=other_id, workspace_id=workspace.id)
        other_context = await store.get_context(owner_id=other_id, workspace_id=other_workspace.id)
        self.assertEqual(other_context["memory_cursor"], 0)

        concurrent = await asyncio.gather(
            *[
                store.record_event(
                    owner_id=owner_id,
                    workspace_id=workspace.id,
                    operation_id=f"mcp:search:{index}",
                    kind="workspace.inspected",
                    summary=f"ChatGPT completed bounded search {index}.",
                )
                for index in range(8)
            ]
        )
        sequences = sorted(item["event"]["sequence"] for item in concurrent)
        self.assertEqual(sequences, list(range(3, 11)))

        timeline = await store.list_events(owner_id=owner_id, workspace_id=workspace.id, limit=100)
        self.assertEqual([event["sequence"] for event in timeline["events"]], list(range(1, 11)))

        cleared = await store.clear(owner_id=owner_id, workspace_id=workspace.id)
        self.assertEqual(cleared["cursor"], 0)
        cleared_timeline = await store.list_events(owner_id=owner_id, workspace_id=workspace.id)
        self.assertEqual(cleared_timeline["events"], [])
        cleared_facts = await store.list_facts(owner_id=owner_id, workspace_id=workspace.id)
        self.assertEqual(cleared_facts["facts"], [])


if __name__ == "__main__":
    unittest.main()
