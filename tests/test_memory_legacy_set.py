import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.utils.memory import MemoryRoot, apply_markdown_memory_batch, remember


class LegacyMemorySetTests(unittest.IsolatedAsyncioTestCase):
    def test_markdown_set_replaces_file_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_path = Path(tmp)
            baseline = root_path / "MEMORY.md"
            baseline.write_text("old content\n")
            root = MemoryRoot("user", root_path, baseline)

            result = apply_markdown_memory_batch(
                root,
                [{"action": "set", "path": "MEMORY.md", "content": "new durable fact"}],
            )

            self.assertTrue(result["success"])
            self.assertEqual(baseline.read_text(), "new durable fact\n")

    async def test_remember_normalizes_legacy_type_set_and_queues_canonical_memory(self):
        request = SimpleNamespace()
        service = SimpleNamespace(queue_consolidation=AsyncMock(return_value="job-1"))
        with (
            patch(
                "cptr.utils.memory.get_memory_settings",
                new=AsyncMock(return_value={"enabled": True}),
            ),
            patch(
                "cptr.utils.memory.write_memory",
                new=AsyncMock(
                    return_value={
                        "success": True,
                        "message": "set memory file MEMORY.md",
                        "path": "/memory",
                    }
                ),
            ) as write_memory,
            patch(
                "cptr.utils.memory._record_memory_fabric_event",
                new=AsyncMock(return_value="event-1"),
            ),
            patch("cptr.memory.service.get_memory_service", return_value=service),
        ):
            result = await remember(
                request,
                user_id="user-1",
                workspace="/repo",
                scope="workspace",
                operations=[{"type": "set", "path": "MEMORY.md", "content": "durable fact"}],
            )

        self.assertTrue(result["success"])
        normalized = write_memory.await_args.args[4]
        self.assertEqual(normalized[0]["action"], "set")
        service.queue_consolidation.assert_awaited_once()
        self.assertEqual(service.queue_consolidation.await_args.kwargs["text"], "durable fact")
        self.assertEqual(
            service.queue_consolidation.await_args.kwargs["source_event_ids"], ["event-1"]
        )


if __name__ == "__main__":
    unittest.main()
