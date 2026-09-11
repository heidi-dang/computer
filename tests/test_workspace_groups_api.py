"""Tests for WorkspaceGroup REST API router endpoints."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from cptr.routers.workspace_groups import (
    AddWorkspaceGroupMemberRequest,
    CreateWorkspaceGroupRequest,
    ReorderMembersRequest,
    UpdateWorkspaceGroupMemberRequest,
    UpdateWorkspaceGroupRequest,
    add_member,
    create_group,
    delete_group,
    get_group,
    list_groups,
    remove_member,
    reorder_members,
    router as workspace_groups_router,
    update_group,
    update_member,
)
from cptr.services.workspace_groups import (
    WorkspaceGroupDuplicateMemberError,
    WorkspaceGroupNotFoundError,
    WorkspaceGroupSlugConflictError,
    WorkspaceGroupValidationError,
)


class WorkspaceGroupsApiContractTests(unittest.TestCase):
    def test_routes_registered(self) -> None:
        """Verify all expected routes exist on the workspace_groups_router."""
        paths_and_methods = {
            (route.path, tuple(route.methods or ())) for route in workspace_groups_router.routes
        }
        self.assertIn(("/api/workspace-groups", ("POST",)), paths_and_methods)
        self.assertIn(("/api/workspace-groups", ("GET",)), paths_and_methods)
        self.assertIn(("/api/workspace-groups/{group_ref}", ("GET",)), paths_and_methods)
        self.assertIn(("/api/workspace-groups/{group_ref}", ("PATCH",)), paths_and_methods)
        self.assertIn(("/api/workspace-groups/{group_ref}", ("DELETE",)), paths_and_methods)
        self.assertIn(("/api/workspace-groups/{group_ref}/members", ("POST",)), paths_and_methods)
        self.assertIn(
            ("/api/workspace-groups/{group_ref}/members/{workspace_ref}", ("DELETE",)),
            paths_and_methods,
        )
        self.assertIn(
            ("/api/workspace-groups/{group_ref}/members/{workspace_ref}", ("PATCH",)),
            paths_and_methods,
        )
        self.assertIn(("/api/workspace-groups/{group_ref}/order", ("PUT",)), paths_and_methods)


class WorkspaceGroupsApiBehaviorTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request(user_id: str | None = "user-1", bearer: str | None = None) -> SimpleNamespace:
        headers = {}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        auth = SimpleNamespace(user_id=user_id, role="user") if user_id else None
        return SimpleNamespace(
            headers=headers,
            state=SimpleNamespace(auth=auth),
        )

    async def test_auth_rejection(self) -> None:
        """Verify unauthenticated request raises 401."""
        req = self._request(user_id=None)
        with self.assertRaises(HTTPException) as exc:
            await list_groups(req)
        self.assertEqual(exc.exception.status_code, 401)

    async def test_create_group_endpoint(self) -> None:
        """Verify create_group endpoint invokes service with owner user_id."""
        req = self._request("user-1")
        body = CreateWorkspaceGroupRequest(
            name="Cross Repo Suite",
            slug="cross-repo-suite",
            description="Multi-repo project",
            group_type="cross-repo",
            config={"shared": True},
            members=[{"workspace_id": "ws-1", "role": "primary", "primary": True}],
        )
        fake_created = {
            "id": "grp-1",
            "user_id": "user-1",
            "name": "Cross Repo Suite",
            "slug": "cross-repo-suite",
            "member_count": 1,
            "members": [{"workspace_id": "ws-1", "primary": True}],
        }
        with patch(
            "cptr.routers.workspace_groups.WorkspaceGroupService.create_group",
            new=AsyncMock(return_value=fake_created),
        ) as mock_create:
            res = await create_group(req, body)
            mock_create.assert_awaited_once_with(
                user_id="user-1",
                name="Cross Repo Suite",
                slug="cross-repo-suite",
                description="Multi-repo project",
                group_type="cross-repo",
                config={"shared": True},
                members=[{"workspace_id": "ws-1", "role": "primary", "primary": True}],
            )
            self.assertEqual(res, fake_created)

    async def test_duplicate_slug_returns_409(self) -> None:
        """Verify duplicate group slug raises HTTP 409."""
        req = self._request("user-1")
        body = CreateWorkspaceGroupRequest(name="Duplicate", slug="existing-slug")
        with patch(
            "cptr.routers.workspace_groups.WorkspaceGroupService.create_group",
            side_effect=WorkspaceGroupSlugConflictError("group slug already in use: existing-slug"),
        ):
            with self.assertRaises(HTTPException) as exc:
                await create_group(req, body)
            self.assertEqual(exc.exception.status_code, 409)
            self.assertEqual(exc.exception.detail["code"], "WORKSPACE_GROUP_SLUG_CONFLICT")

    async def test_duplicate_member_returns_409(self) -> None:
        """Verify duplicate member addition raises HTTP 409."""
        req = self._request("user-1")
        body = AddWorkspaceGroupMemberRequest(workspace_id="ws-1")
        with patch(
            "cptr.routers.workspace_groups.WorkspaceGroupService.add_member",
            side_effect=WorkspaceGroupDuplicateMemberError("workspace is already a member"),
        ):
            with self.assertRaises(HTTPException) as exc:
                await add_member(req, "grp-1", body)
            self.assertEqual(exc.exception.status_code, 409)
            self.assertEqual(exc.exception.detail["code"], "DUPLICATE_GROUP_MEMBER")

    async def test_get_nonexistent_group_returns_404(self) -> None:
        """Verify fetching non-existent group returns 404."""
        req = self._request("user-1")
        with patch(
            "cptr.routers.workspace_groups.WorkspaceGroupService.get_group",
            side_effect=WorkspaceGroupNotFoundError("workspace group not found: missing"),
        ):
            with self.assertRaises(HTTPException) as exc:
                await get_group(req, "missing")
            self.assertEqual(exc.exception.status_code, 404)
            self.assertEqual(exc.exception.detail["code"], "WORKSPACE_GROUP_NOT_FOUND")

    async def test_reorder_members_endpoint(self) -> None:
        """Verify reorder members endpoint passes ordered IDs and returns updated group."""
        req = self._request("user-1")
        body = ReorderMembersRequest(workspace_ids=["ws-3", "ws-1", "ws-2"])
        fake_reordered = {
            "id": "grp-1",
            "members": [
                {"workspace_id": "ws-3"},
                {"workspace_id": "ws-1"},
                {"workspace_id": "ws-2"},
            ],
        }
        with patch(
            "cptr.routers.workspace_groups.WorkspaceGroupService.reorder_members",
            new=AsyncMock(return_value=fake_reordered),
        ) as mock_reorder:
            res = await reorder_members(req, "grp-1", body)
            mock_reorder.assert_awaited_once_with(
                user_id="user-1",
                group_ref="grp-1",
                ordered_workspace_refs=["ws-3", "ws-1", "ws-2"],
            )
            self.assertEqual(res, fake_reordered)

    async def test_update_and_remove_member_endpoints(self) -> None:
        """Verify update_member and remove_member endpoints."""
        req = self._request("user-1")
        body = UpdateWorkspaceGroupMemberRequest(role="lead", primary=True, alias="core")
        fake_updated = {
            "id": "grp-1",
            "members": [{"workspace_id": "ws-1", "primary": True, "role": "lead"}],
        }
        with patch(
            "cptr.routers.workspace_groups.WorkspaceGroupService.update_member",
            new=AsyncMock(return_value=fake_updated),
        ) as mock_update_member:
            res = await update_member(req, "grp-1", "ws-1", body)
            mock_update_member.assert_awaited_once_with(
                user_id="user-1",
                group_ref="grp-1",
                workspace_ref="ws-1",
                role="lead",
                primary=True,
                alias="core",
                sort_order=None,
                enabled=None,
                config=None,
            )
            self.assertEqual(res, fake_updated)

        with patch(
            "cptr.routers.workspace_groups.WorkspaceGroupService.remove_member",
            new=AsyncMock(return_value={"id": "grp-1", "members": []}),
        ) as mock_remove:
            res = await remove_member(req, "grp-1", "ws-1")
            mock_remove.assert_awaited_once_with(
                user_id="user-1",
                group_ref="grp-1",
                workspace_ref="ws-1",
            )
            self.assertEqual(res["id"], "grp-1")

    async def test_validation_error_returns_422(self) -> None:
        """Verify WorkspaceGroupValidationError raises HTTP 422."""
        req = self._request("user-1")
        body = CreateWorkspaceGroupRequest(name="Invalid")
        with patch(
            "cptr.routers.workspace_groups.WorkspaceGroupService.create_group",
            side_effect=WorkspaceGroupValidationError("group name cannot be blank"),
        ):
            with self.assertRaises(HTTPException) as exc:
                await create_group(req, body)
            self.assertEqual(exc.exception.status_code, 422)
            self.assertEqual(exc.exception.detail["code"], "WORKSPACE_GROUP_VALIDATION_ERROR")

    async def test_delete_and_update_endpoints(self) -> None:
        """Verify update and delete endpoints."""
        req = self._request("user-1")
        update_body = UpdateWorkspaceGroupRequest(
            name="New Name", description="Updated description"
        )
        with patch(
            "cptr.routers.workspace_groups.WorkspaceGroupService.update_group",
            new=AsyncMock(return_value={"id": "grp-1", "name": "New Name"}),
        ) as mock_update:
            res = await update_group(req, "grp-1", update_body)
            mock_update.assert_awaited_once_with(
                user_id="user-1",
                group_ref="grp-1",
                name="New Name",
                slug=None,
                description="Updated description",
                group_type=None,
                config=None,
            )
            self.assertEqual(res["name"], "New Name")

        with patch(
            "cptr.routers.workspace_groups.WorkspaceGroupService.delete_group",
            new=AsyncMock(return_value={"deleted": True, "id": "grp-1"}),
        ) as mock_delete:
            res = await delete_group(req, "grp-1")
            mock_delete.assert_awaited_once_with(user_id="user-1", group_ref="grp-1")
            self.assertTrue(res["deleted"])


if __name__ == "__main__":
    unittest.main()
