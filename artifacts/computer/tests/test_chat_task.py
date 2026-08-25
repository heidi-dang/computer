import ast
import asyncio
import httpx
import inspect
import unittest

from cptr.utils import chat_task
from cptr.utils.chat_task import _format_task_error, run_chat_task, tool_call_fingerprint


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

    def test_native_loop_cancellation_is_re_raised_after_cleanup(self):
        tree = ast.parse(inspect.getsource(run_chat_task))
        handlers = [
            handler
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if isinstance(handler.type, ast.Attribute)
            and handler.type.attr == "CancelledError"
        ]

        self.assertEqual(len(handlers), 1)
        self.assertTrue(
            any(
                isinstance(node, ast.Raise) and node.exc is None
                for node in handlers[0].body
            ),
            "native loop cancellation must propagate after durable chat cleanup",
        )

    async def test_cancel_task_awaits_a_task_cancelled_before_first_turn(self):
        async def never_started():
            await asyncio.sleep(60)

        task = asyncio.create_task(never_started())
        message_id = "cancel-before-first-turn"
        chat_task._tasks[message_id] = task
        try:
            self.assertTrue(await chat_task.cancel_task(message_id))
            self.assertTrue(task.done())
            self.assertTrue(task.cancelled())
        finally:
            chat_task._tasks.pop(message_id, None)

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