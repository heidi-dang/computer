import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from cptr.memory.domain import MemoryContextBundle
from cptr.routers.control import WorkspaceActionRequest, workspace_os_action
from cptr.services.workspace_actions import WorkspaceActionError, WorkspaceActionService
from cptr.services.workspace_groups import WorkspaceGroupDuplicateMemberError
from cptr.services.workspace_resolver import ResolutionResult, ResolutionStage


def _workspace(root: str, *, workspace_id: str = "ws-1", name: str = "Demo"):
    return SimpleNamespace(
        id=workspace_id,
        user_id="user-1",
        path=root,
        name=name,
        slug=name.lower(),
        workspace_type="project",
        data={},
        created_at=1,
        updated_at=2,
    )


class WorkspaceActionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_preserves_authoritative_match_provenance(self):
        service = WorkspaceActionService()
        with tempfile.TemporaryDirectory() as temp:
            workspace = _workspace(temp)
            service._aliases = AsyncMock(return_value=[])
            service._resolver.resolve_detailed_for_user = AsyncMock(
                return_value=ResolutionResult(
                    workspace=workspace,
                    stage=ResolutionStage.SLUG,
                    query="demo",
                    confidence=1.0,
                    metadata={"source": "resolver"},
                )
            )

            value = await service.resolve(user_id="user-1", reference="demo")

        self.assertEqual(value["workspace"]["workspace_id"], "ws-1")
        self.assertEqual(value["matched_by"], "slug")
        self.assertEqual(value["confidence"], 1.0)
        service._resolver.resolve_detailed_for_user.assert_awaited_once()

    async def test_memory_context_read_does_not_create_checkpoint(self):
        service = WorkspaceActionService()
        captured = []
        memory = SimpleNamespace()

        async def prepare_context(value):
            captured.append(value)
            return MemoryContextBundle(
                context_id="memctx-1",
                status="ok",
                memory_version=7,
                rendered="remembered",
            )

        memory.prepare_context = AsyncMock(side_effect=prepare_context)
        namespace = SimpleNamespace(workspace_id="ws-1")

        with (
            patch(
                "cptr.services.workspace_actions.resolve_workspace_namespace",
                new=AsyncMock(return_value=namespace),
            ),
            patch(
                "cptr.services.workspace_actions.get_memory_service",
                return_value=memory,
            ),
        ):
            bundle, diagnostics = await service._memory_input(
                user_id="user-1",
                workspace=_workspace("/tmp", workspace_id="ws-1"),
                current_message="inspect context",
                max_chars=3000,
            )

        self.assertEqual(bundle.memory_version, 7)
        self.assertEqual(diagnostics, [])
        self.assertEqual(captured[0].workspace, "ws-1")
        self.assertEqual(
            captured[0].task_key,
            "",
            "read-only workspace.context must not create a Memory Core checkpoint",
        )

    async def test_explicit_workspace_must_match_bound_workbench_workspace(self):
        service = WorkspaceActionService()
        service._resolve = AsyncMock(
            side_effect=[
                _workspace("/tmp/a", workspace_id="ws-a", name="A"),
                _workspace("/tmp/b", workspace_id="ws-b", name="B"),
            ]
        )

        with self.assertRaises(WorkspaceActionError) as caught:
            await service._context_reference(
                user_id="user-1",
                reference="A",
                workspace_id=None,
                workbench={"workspace_id": "B", "active_workspace_id": None},
            )

        self.assertEqual(caught.exception.code, "WORKSPACE_WORKBENCH_MISMATCH")
        self.assertEqual(caught.exception.status_code, 409)

    async def test_group_domain_errors_are_normalized(self):
        service = WorkspaceActionService()
        with patch(
            "cptr.services.workspace_actions.WorkspaceGroupService.add_member",
            new=AsyncMock(side_effect=WorkspaceGroupDuplicateMemberError("already present")),
        ):
            with self.assertRaises(WorkspaceActionError) as caught:
                await service.execute(
                    user_id="user-1",
                    action="group_add_member",
                    payload={
                        "group_ref": "cross-repo",
                        "workspace_ref": "computer",
                    },
                )

        self.assertEqual(caught.exception.code, "DUPLICATE_GROUP_MEMBER")
        self.assertEqual(caught.exception.status_code, 409)

    async def test_unsupported_action_fails_closed(self):
        service = WorkspaceActionService()
        with self.assertRaises(WorkspaceActionError) as caught:
            await service.execute(
                user_id="user-1",
                action="delete_everything",
                payload={},
            )
        self.assertEqual(caught.exception.code, "WORKSPACE_ACTION_UNSUPPORTED")
        self.assertEqual(caught.exception.status_code, 422)


class WorkspaceActionRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_action_accepts_owner_session_or_workspace_read_scope(self):
        request = SimpleNamespace()
        with (
            patch(
                "cptr.routers.control.require_owner_session_or_control_user",
                new=AsyncMock(return_value="user-1"),
            ) as auth,
            patch(
                "cptr.routers.control.workspace_action_service.execute",
                new=AsyncMock(return_value={"ok": True}),
            ) as execute,
        ):
            result = await workspace_os_action(
                request,
                WorkspaceActionRequest(
                    action="health",
                    payload={"workspace_id": "ws-1"},
                ),
            )

        self.assertEqual(result, {"ok": True})
        auth.assert_awaited_once_with(request, "workspace:read")
        execute.assert_awaited_once_with(
            user_id="user-1",
            action="health",
            payload={"workspace_id": "ws-1"},
        )

    async def test_metadata_mutation_accepts_owner_session_or_workspace_write_scope(self):
        request = SimpleNamespace()
        with (
            patch(
                "cptr.routers.control.require_owner_session_or_control_user",
                new=AsyncMock(return_value="user-1"),
            ) as auth,
            patch(
                "cptr.routers.control.workspace_action_service.execute",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            await workspace_os_action(
                request,
                WorkspaceActionRequest(
                    action="group_update",
                    payload={"group_ref": "cross-repo", "name": "Cross Repo"},
                ),
            )

        auth.assert_awaited_once_with(request, "workspace:write")

    async def test_unsupported_action_is_rejected_before_authentication(self):
        request = SimpleNamespace()
        with patch(
            "cptr.routers.control.require_owner_session_or_control_user",
            new=AsyncMock(return_value="user-1"),
        ) as auth:
            with self.assertRaises(HTTPException) as caught:
                await workspace_os_action(
                    request,
                    WorkspaceActionRequest(action="unknown", payload={}),
                )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(
            caught.exception.detail["code"],
            "WORKSPACE_ACTION_UNSUPPORTED",
        )
        auth.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
