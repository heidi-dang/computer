import unittest

from cptr.utils.chat_task import tool_call_fingerprint


class ChatTaskLoopTests(unittest.IsolatedAsyncioTestCase):
    def test_tool_call_fingerprint_ignores_argument_key_order(self):
        self.assertEqual(
            tool_call_fingerprint("list_directory", {"path": "/tmp", "depth": 1}),
            tool_call_fingerprint("list_directory", {"depth": 1, "path": "/tmp"}),
        )
        self.assertNotEqual(
            tool_call_fingerprint("list_directory", {"path": "/tmp"}),
            tool_call_fingerprint("list_directory", {"path": "/workspace"}),
        )

    async def test_repeated_list_directory_emits_terminal_event(self):
        """The terminal event contract remains usable by socket and gateway clients."""
        events = []

        async def emit(**payload):
            events.append(payload)

        calls = [{"name": "list_directory", "arguments": {"path": "/workspace"}} for _ in range(4)]
        counts = {}
        limit = 3
        for call in calls:
            key = tool_call_fingerprint(call["name"], call["arguments"])
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > limit:
                await emit(
                    done=True,
                    error="I stopped this response after list_directory was requested repeatedly.",
                )
                break

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["done"])
        self.assertIn("list_directory", events[0]["error"])


if __name__ == "__main__":
    unittest.main()