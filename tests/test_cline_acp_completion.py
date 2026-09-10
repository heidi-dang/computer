import asyncio
import unittest
from unittest.mock import patch

from cptr.utils.agents.attachments import PreparedAgentAttachments
from cptr.utils.agents.cline import run_cline_agent
from cptr.utils.agents.cursor import run_cursor_agent
from cptr.utils.agents.events import AgentDone, AgentTextDelta
from cptr.utils.agents.gemini import run_gemini_agent
from cptr.utils.agents.grok import run_grok_agent


ADAPTERS = (
    (
        "hermes-cline",
        "cptr.utils.agents.cline.AcpClient",
        run_cline_agent,
        {"id": "hermes-agent", "command": "hermes-agent-cptr"},
    ),
    (
        "cursor",
        "cptr.utils.agents.cursor.AcpClient",
        run_cursor_agent,
        {"id": "cursor", "command": "agent"},
    ),
    (
        "grok",
        "cptr.utils.agents.grok.AcpClient",
        run_grok_agent,
        {"id": "grok", "command": "grok"},
    ),
    (
        "gemini",
        "cptr.utils.agents.gemini.AcpClient",
        run_gemini_agent,
        {"id": "gemini", "command": "gemini"},
    ),
)


class _PromptCompletesWithoutTrailingEventClient:
    instances = []

    def __init__(self, **kwargs):
        self.events = asyncio.Queue()
        self.session_id = "session-1"
        self.closed = False
        self.cancelled = False
        type(self).instances.append(self)

    async def start(self):
        return None

    async def set_model(self, model):
        return None

    async def prompt(self, text, images=None):
        await asyncio.sleep(0)
        return {"stopReason": "end_turn"}

    async def cancel(self):
        self.cancelled = True

    async def close(self):
        self.closed = True


class _FinalEventThenPromptCompletesClient(_PromptCompletesWithoutTrailingEventClient):
    async def prompt(self, text, images=None):
        await self.events.put(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "finished"},
                    }
                },
            }
        )
        await asyncio.sleep(0)
        return {"stopReason": "end_turn"}


class _TrackedQueue(asyncio.Queue):
    def __init__(self):
        super().__init__()
        self.active_gets = 0

    async def get(self):
        self.active_gets += 1
        try:
            return await super().get()
        finally:
            self.active_gets -= 1


class _NeverCompletesClient(_PromptCompletesWithoutTrailingEventClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.events = _TrackedQueue()

    async def prompt(self, text, images=None) -> dict[str, str]:
        await asyncio.Future()
        raise AssertionError("unreachable")


class AcpAdapterCompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _PromptCompletesWithoutTrailingEventClient.instances.clear()
        _FinalEventThenPromptCompletesClient.instances.clear()

    async def _collect(self, patch_path, runner, profile, client_type):
        with patch(patch_path, client_type):
            return [
                event
                async for event in runner(
                    profile=profile,
                    model="test-model",
                    workspace="/tmp",
                    messages=[{"role": "user", "content": "finish the turn"}],
                    system_prompt="",
                    chat_params={},
                    resume_state=None,
                    attachments=PreparedAgentAttachments(images=[], files=[], prompt_suffix=""),
                )
            ]

    async def test_prompt_response_completes_without_trailing_acp_event(self):
        for name, patch_path, runner, profile in ADAPTERS:
            with self.subTest(adapter=name):
                events = await asyncio.wait_for(
                    self._collect(
                        patch_path,
                        runner,
                        profile,
                        _PromptCompletesWithoutTrailingEventClient,
                    ),
                    timeout=0.15,
                )
                self.assertIsInstance(events[-1], AgentDone)
                client = _PromptCompletesWithoutTrailingEventClient.instances[-1]
                self.assertTrue(client.closed)
                self.assertFalse(client.cancelled)

    async def test_final_queued_update_is_emitted_before_done(self):
        for name, patch_path, runner, profile in ADAPTERS:
            with self.subTest(adapter=name):
                events = await asyncio.wait_for(
                    self._collect(
                        patch_path,
                        runner,
                        profile,
                        _FinalEventThenPromptCompletesClient,
                    ),
                    timeout=0.15,
                )
                self.assertEqual(
                    [event.text for event in events if isinstance(event, AgentTextDelta)],
                    ["finished"],
                )
                self.assertIsInstance(events[-1], AgentDone)

    async def test_cancellation_cleans_pending_event_wait(self):
        patch_path = "cptr.utils.agents.cline.AcpClient"
        profile = {"id": "hermes-agent", "command": "hermes-agent-cptr"}
        task = asyncio.create_task(
            self._collect(patch_path, run_cline_agent, profile, _NeverCompletesClient)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        client = _NeverCompletesClient.instances[-1]
        self.assertEqual(client.events.active_gets, 0)
        self.assertTrue(client.cancelled)
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
