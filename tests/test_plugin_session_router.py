from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

from fastapi import HTTPException

from cptr.routers.plugin import (
    AppendWorkbenchSessionEventRequest,
    AppendWorkspaceMemoryEventRequest,
    ClearWorkspaceMemoryRequest,
    _after_sequence,
    _ensure_target_owner,
    append_control_workbench_session_event,
    append_control_workspace_memory_event,
    clear_control_workspace_memory,
)


class PluginSessionRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_event_rejects_partial_target_reference_before_persisting(self):
        body = AppendWorkbenchSessionEventRequest(target_type="task", summary="activity")
        request = SimpleNamespace()
        with patch("cptr.routers.plugin._control_user", new=AsyncMock(return_value="user_1")), patch(
            "cptr.routers.plugin.publish_workbench_session_event", new=AsyncMock()
        ) as publish:
            with self.assertRaises(HTTPException) as error:
                await append_control_workbench_session_event(request, "wbs_example", body)

        self.assertEqual(error.exception.status_code, 422)
        publish.assert_not_awaited()

    async def test_plugin_event_validates_owner_target_before_persisting(self):
        body = AppendWorkbenchSessionEventRequest(
            target_type="command",
            target_id="cmd_1",
            workspace_id="ws_1",
            summary="command started",
        )
        request = SimpleNamespace()
        envelope = SimpleNamespace(event={"session_id": "wbs_example", "sequence": 1})
        with patch("cptr.routers.plugin._control_user", new=AsyncMock(return_value="user_1")), patch(
            "cptr.routers.plugin._ensure_target_owner", new=AsyncMock()
        ) as ensure_target, patch(
            "cptr.routers.plugin.publish_workbench_session_event", new=AsyncMock(return_value=envelope)
        ) as publish:
            result = await append_control_workbench_session_event(request, "wbs_example", body)

        self.assertEqual(result, envelope.event)
        ensure_target.assert_awaited_once_with("user_1", "command", "cmd_1", "ws_1")
        self.assertEqual(publish.await_args.kwargs["owner_id"], "user_1")
        self.assertEqual(publish.await_args.kwargs["target_id"], "cmd_1")

    async def test_command_target_binding_requires_owner_and_matching_live_workspace(self):
        owned_command = {
            "user_id": "user_1",
            "live_target": {"target_type": "command", "workspace_id": "ws_1"},
        }
        with patch("cptr.routers.plugin._ensure_workspace_owner", new=AsyncMock()) as workspace, patch(
            "cptr.routers.plugin.get_command_session", return_value=owned_command
        ):
            await _ensure_target_owner("user_1", "command", "cmd_1", "ws_1")
        workspace.assert_awaited_once_with("user_1", "ws_1")

        with patch("cptr.routers.plugin._ensure_workspace_owner", new=AsyncMock()), patch(
            "cptr.routers.plugin.get_command_session", return_value=None
        ):
            with self.assertRaises(HTTPException) as error:
                await _ensure_target_owner("user_1", "command", "missing", "ws_1")
        self.assertEqual(error.exception.status_code, 404)

    async def test_workspace_memory_event_uses_control_owner_and_cannot_bypass_workspace_scope(self):
        body = AppendWorkspaceMemoryEventRequest(
            workspace_id="ws_1",
            operation_id="mcp:read:1",
            kind="workspace.inspected",
            summary="Read a bounded source file.",
        )
        request = SimpleNamespace()
        expected = {"event": {"event_id": "wme_1", "sequence": 1}, "idempotent": False}
        with patch("cptr.routers.plugin._control_user", new=AsyncMock(return_value="user_1")), patch(
            "cptr.routers.plugin._record_workspace_memory_event", new=AsyncMock(return_value=expected)
        ) as record:
            result = await append_control_workspace_memory_event(request, body)

        self.assertEqual(result, expected)
        record.assert_awaited_once_with("user_1", body)

    async def test_workspace_memory_clear_requires_explicit_confirmation(self):
        request = SimpleNamespace()
        body = ClearWorkspaceMemoryRequest(workspace_id="ws_1", confirm=False)
        with patch("cptr.routers.plugin._control_user", new=AsyncMock()) as auth:
            with self.assertRaises(HTTPException) as error:
                await clear_control_workspace_memory(request, body)
        self.assertEqual(error.exception.status_code, 422)
        auth.assert_not_awaited()

    def test_plugin_session_cursor_rejects_malformed_last_event_id(self):
        request = SimpleNamespace(headers={"last-event-id": "wbs_example:not-a-number"})
        with self.assertRaises(HTTPException) as error:
            _after_sequence(request, 0)
        self.assertEqual(error.exception.status_code, 400)
