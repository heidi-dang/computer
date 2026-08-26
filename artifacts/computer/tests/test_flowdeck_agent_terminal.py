import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cptr.flowdeck.agent_terminal import (
    close_agent_terminal,
    execute_agent_terminal_command,
)
from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import FlowDeckMode


class FlowDeckAgentTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await close_agent_terminal("terminal-test-run", "terminal-test-user")

    def enabled_env(self):
        return {
            "CPTR_FLOWDECK_ENABLED": "1",
            "CPTR_FLOWDECK_MODE": "controlled",
            "CPTR_FLOWDECK_GOVERNANCE": "strict",
            "CPTR_FLOWDECK_MUTATING_AGENTS": "1",
            "CPTR_FLOWDECK_AGENT_TERMINAL_ENABLED": "1",
            "CPTR_FLOWDECK_MAX_TERMINAL_TIMEOUT_SECONDS": "3",
        }

    async def observe_noop(self, *_args):
        return None

    async def test_disabled_by_default_and_configured_limits_are_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(FlowDeckConfig.from_env().agent_terminal_enabled)
            result = await execute_agent_terminal_command(
                "printf nope",
                __context__={
                    "user_id": "terminal-test-user",
                    "flowdeck_run_id": "terminal-test-run",
                    "workspace": tempfile.gettempdir(),
                },
            )
        self.assertIn("disabled", result.lower())

    async def test_persistent_cwd_and_authoritative_exit_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            child = root / "child"
            child.mkdir()
            events = []

            async def observe(kind, payload):
                events.append((kind, payload))

            with patch.dict(os.environ, self.enabled_env(), clear=False):
                context = {
                    "user_id": "terminal-test-user",
                    "flowdeck_run_id": "terminal-test-run",
                    "workspace": str(root),
                    "request": None,
                    "terminal_observer": observe,
                }
                first = json.loads(await execute_agent_terminal_command("printf ready", __context__=context))
                second = json.loads(
                    await execute_agent_terminal_command(
                        "cd child && pwd",
                        __context__=context,
                    )
                )
                third = json.loads(
                    await execute_agent_terminal_command("pwd", __context__=context)
                )
                failed = json.loads(
                    await execute_agent_terminal_command(
                        "printf failure >&2; false",
                        __context__=context,
                    )
                )
                redacted = json.loads(
                    await execute_agent_terminal_command(
                        "printf 'token=super-secret'",
                        __context__=context,
                    )
                )

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(third["status"], "succeeded")
        self.assertEqual(Path(third["output"].strip()), child)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["exit_code"], 1)
        self.assertIn("[REDACTED]", redacted["output"])
        self.assertNotIn("super-secret", redacted["output"])
        self.assertIn("command_start", [kind for kind, _ in events])
        self.assertIn("command_exit", [kind for kind, _ in events])
        self.assertTrue(
            all(
                "__CPTR_AGENT_" not in str(payload.get("text", ""))
                for kind, payload in events
                if kind == "command_output"
            )
        )

    async def test_rejects_dangerous_and_out_of_workspace_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            context = {
                "user_id": "terminal-test-user",
                "flowdeck_run_id": "terminal-test-run",
                "workspace": temp,
                "request": None,
                "terminal_observer": self.observe_noop,
            }
            with patch.dict(os.environ, self.enabled_env(), clear=False):
                dangerous = await execute_agent_terminal_command(
                    "rm -rf /",
                    __context__=context,
                )
                outside = await execute_agent_terminal_command(
                    "printf nope",
                    cwd="..",
                    __context__=context,
                )
        self.assertIn("rejected", dangerous.lower())
        self.assertIn("inside", outside.lower())

    async def test_timeout_discards_the_session(self):
        with tempfile.TemporaryDirectory() as temp:
            context = {
                "user_id": "terminal-test-user",
                "flowdeck_run_id": "terminal-test-run",
                "workspace": temp,
                "request": None,
                "terminal_observer": self.observe_noop,
            }
            with patch.dict(os.environ, self.enabled_env(), clear=False):
                result = json.loads(
                    await execute_agent_terminal_command(
                        "sleep 10",
                        timeout_seconds=1,
                        __context__=context,
                    )
                )
        self.assertEqual(result["status"], "timed_out")