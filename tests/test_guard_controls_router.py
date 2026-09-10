import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from cptr.routers.guard_controls import (
    GuardResetRequest,
    GuardUpdateRequest,
    get_control_guards,
    get_mcp_guards,
    reset_mcp_guards,
    update_mcp_guard,
)
from cptr.services.guard_controls import GuardImmutableError, GuardNotFoundError, GuardVersionConflict


class GuardControlsRouterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _browser_request(user_id: str | None = "user-1"):
        auth = SimpleNamespace(user_id=user_id, role="admin") if user_id else None
        return SimpleNamespace(state=SimpleNamespace(auth=auth))

    async def test_browser_catalog_uses_authenticated_owner_and_exposes_host_capability(self):
        catalog = {"guards": [], "mutable_count": 0, "enabled_mutable_count": 0, "locked_count": 0}
        with (
            patch("cptr.routers.guard_controls.guard_policy_service.catalog", new=AsyncMock(return_value=catalog)) as read,
            patch("cptr.routers.guard_controls.local_root_grants_enabled", return_value=True),
        ):
            result = await get_mcp_guards(self._browser_request())
        read.assert_awaited_once_with("user-1")
        self.assertTrue(result["host_capabilities"]["local_root_grants"])

    async def test_browser_catalog_requires_owner_session(self):
        with self.assertRaises(HTTPException) as denied:
            await get_mcp_guards(self._browser_request(None))
        self.assertEqual(denied.exception.status_code, 401)

    async def test_control_projection_requires_existing_coding_read_scope_and_is_read_only(self):
        request = SimpleNamespace(state=SimpleNamespace())
        catalog = {"guards": [{"id": "x"}]}
        with (
            patch("cptr.routers.guard_controls.require_control_user", new=AsyncMock(return_value="user-2")) as auth,
            patch("cptr.routers.guard_controls.guard_policy_service.catalog", new=AsyncMock(return_value=catalog)) as read,
        ):
            result = await get_control_guards(request)
        auth.assert_awaited_once_with(request, "coding:read")
        read.assert_awaited_once_with("user-2")
        self.assertEqual(result, catalog)

    async def test_update_passes_expected_version_and_owner_only(self):
        request = self._browser_request()
        changed = {"id": "delegation_prompt_approval", "enabled": False, "version": 4}
        with patch(
            "cptr.routers.guard_controls.guard_policy_service.set_guard",
            new=AsyncMock(return_value=changed),
        ) as update:
            result = await update_mcp_guard(
                request,
                "delegation_prompt_approval",
                GuardUpdateRequest(enabled=False, expected_version=3),
            )
        update.assert_awaited_once_with(
            "user-1",
            "delegation_prompt_approval",
            enabled=False,
            expected_version=3,
            source="mcp_ui",
        )
        self.assertEqual(result, changed)

    async def test_locked_unknown_and_stale_updates_are_structured_conflicts(self):
        request = self._browser_request()
        cases = (
            (GuardNotFoundError("guard not found"), 404, "GUARD_NOT_FOUND"),
            (GuardImmutableError("locked"), 409, "GUARD_IMMUTABLE"),
            (
                GuardVersionConflict({"id": "network_operation_approval", "enabled": False, "version": 5}),
                409,
                "GUARD_VERSION_CONFLICT",
            ),
        )
        for error, status, code in cases:
            with self.subTest(code=code), patch(
                "cptr.routers.guard_controls.guard_policy_service.set_guard",
                new=AsyncMock(side_effect=error),
            ), self.assertRaises(HTTPException) as raised:
                await update_mcp_guard(
                    request,
                    "network_operation_approval",
                    GuardUpdateRequest(enabled=True, expected_version=1),
                )
            self.assertEqual(raised.exception.status_code, status)
            self.assertEqual(raised.exception.detail["code"], code)
            if code == "GUARD_VERSION_CONFLICT":
                self.assertEqual(raised.exception.detail["current"]["version"], 5)

    async def test_reset_uses_owner_and_versions(self):
        request = self._browser_request()
        result = {"guards": [{"id": "x"}]}
        versions = {"delegation_prompt_approval": 2, "secret_write_prompt_approval": 1}
        with patch(
            "cptr.routers.guard_controls.guard_policy_service.reset",
            new=AsyncMock(return_value=result),
        ) as reset:
            actual = await reset_mcp_guards(
                request, GuardResetRequest(expected_versions=versions)
            )
        reset.assert_awaited_once_with(
            "user-1", expected_versions=versions, source="mcp_ui"
        )
        self.assertEqual(actual["guards"], result["guards"])
        self.assertIn("host_capabilities", actual)
        self.assertIn("local_root_grants", actual["host_capabilities"])


if __name__ == "__main__":
    unittest.main()
