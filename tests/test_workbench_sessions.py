import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.routers.workbench import (
    CreateWorkbenchSessionRequest,
    RenameWorkbenchSessionRequest,
    create_workbench_session,
    get_workbench_session_events,
    rename_workbench_session,
    router as workbench_router,
)


class WorkbenchSessionRouterTests(unittest.IsolatedAsyncioTestCase):
    def test_production_app_registers_current_plugin_session_routes(self):
        paths = {route.path for route in workbench_router.routes if hasattr(route, "path")}
        self.assertIn("/api/control/v1/workbench-sessions", paths)
        self.assertIn("/api/control/v1/workbench-sessions/{session_id}", paths)
        self.assertIn("/api/control/v1/workbench-sessions/{session_id}/events", paths)
        self.assertIn("/api/control/v1/workbench-sessions/{session_id}/bind", paths)
        self.assertIn("/api/control/v1/workbench-sessions/{session_id}/delete-request", paths)
        self.assertIn("/api/control/v1/workbench-sessions/delete-confirm", paths)

    async def test_create_session_records_safe_open_event(self):
        request = SimpleNamespace()
        initial = {
            "session_id": "wbs_1",
            "name": "Release work",
            "workspace_id": "ws_1",
            "status": "OPEN",
            "event_count": 0,
        }
        current = {**initial, "event_count": 1}
        with (
            patch("cptr.routers.workbench._user", new=AsyncMock(return_value="user_1")),
            patch(
                "cptr.routers.workbench._ensure_workspace_owner",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "cptr.routers.workbench.workbench_session_store.create",
                new=AsyncMock(return_value=initial),
            ) as create,
            patch(
                "cptr.routers.workbench.workbench_session_store.append_event",
                new=AsyncMock(return_value={"sequence": 1}),
            ) as append,
            patch(
                "cptr.routers.workbench.workbench_session_store.get",
                new=AsyncMock(return_value=current),
            ),
        ):
            result = await create_workbench_session(
                request,
                CreateWorkbenchSessionRequest(name="Release work", workspace_id="ws_1"),
            )

        self.assertEqual(result["event_count"], 1)
        create.assert_awaited_once_with(owner_id="user_1", name="Release work", workspace_id="ws_1")
        self.assertEqual(append.await_args.kwargs["event_type"], "workbench.opened")

    async def test_events_return_last_sequence_cursor_expected_by_plugin(self):
        request = SimpleNamespace()
        events = [{"sequence": 3, "summary": "done"}]
        with (
            patch("cptr.routers.workbench._user", new=AsyncMock(return_value="user_1")),
            patch(
                "cptr.routers.workbench.workbench_session_store.events",
                new=AsyncMock(return_value=events),
            ),
        ):
            result = await get_workbench_session_events(
                request, "wbs_1", after_sequence=2, limit=20
            )

        self.assertEqual(result["last_sequence"], 3)
        self.assertEqual(result["events"], events)

    async def test_rename_matches_current_patch_contract(self):
        request = SimpleNamespace()
        renamed = {"session_id": "wbs_1", "name": "New name", "status": "OPEN"}
        with (
            patch("cptr.routers.workbench._user", new=AsyncMock(return_value="user_1")),
            patch(
                "cptr.routers.workbench.workbench_session_store.rename",
                new=AsyncMock(return_value=renamed),
            ) as rename,
        ):
            result = await rename_workbench_session(
                request, "wbs_1", RenameWorkbenchSessionRequest(name="New name")
            )

        self.assertEqual(result["name"], "New name")
        rename.assert_awaited_once_with(owner_id="user_1", session_id="wbs_1", name="New name")
