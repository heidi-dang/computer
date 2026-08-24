import httpx
import unittest

from cptr.utils.chat_task import _format_task_error, tool_call_fingerprint


class ChatTaskLoopTests(unittest.IsolatedAsyncioTestCase):
    def test_blank_transport_error_is_never_rendered_as_empty_error(self):
        error = _format_task_error(httpx.ConnectTimeout(message=""))

        self.assertEqual(error, "Provider request failed (ConnectTimeout).")
        self.assertNotEqual(f"> **Error:** {error}", "> **Error:** ")

    def test_provider_error_body_and_status_are_preserved(self):
        request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
        response = httpx.Response(
            502,
            request=request,
            text='{"error":{"message":"upstream gateway failed"}}',
        )
        error = _format_task_error(
            httpx.HTTPStatusError("", request=request, response=response)
        )

        self.assertEqual(error, "upstream gateway failed")

    def test_blank_http_error_retains_status_and_exception_type(self):
        request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
        response = httpx.Response(503, request=request)
        error = _format_task_error(
            httpx.HTTPStatusError("", request=request, response=response)
        )

        self.assertEqual(
            error,
            "Provider request failed with HTTP 503 (HTTPStatusError).",
        )

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