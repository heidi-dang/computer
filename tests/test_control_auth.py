import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.api_keys import ApiKeyPrincipal
from cptr.memory.service import MemoryUnavailableError
from cptr.services.control_auth import ControlMemoryUnavailable, authenticate_control_request


class ControlAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_bearer_token_is_accepted(self):
        request = SimpleNamespace(
            headers={"Authorization": "Bearer secret-token"},
            state=SimpleNamespace(),
        )
        principal = ApiKeyPrincipal(
            user_id="user-1",
            username="tester",
            scopes=frozenset({"workspace:read", "task:read"}),
        )
        with (
            patch("cptr.services.control_auth._hash_key", return_value="hash"),
            patch(
                "cptr.services.control_auth.resolve_api_key_principal",
                new=AsyncMock(return_value=principal),
            ),
        ):
            user_id = await authenticate_control_request(request, "workspace:read")

        self.assertEqual(user_id, "user-1")
        self.assertEqual(request.state.control_scopes, {"workspace:read", "task:read"})
        self.assertEqual(request.state.auth.username, "tester")

    async def test_mutating_scope_requires_memory_gate_after_authentication(self):
        request = SimpleNamespace(
            headers={"Authorization": "Bearer secret-token"},
            state=SimpleNamespace(),
        )
        principal = ApiKeyPrincipal(
            user_id="user-1",
            username="tester",
            scopes=frozenset({"coding:write"}),
        )
        gate = AsyncMock(return_value=SimpleNamespace(context_id="memctx-1"))
        with (
            patch("cptr.services.control_auth._hash_key", return_value="hash"),
            patch(
                "cptr.services.control_auth.resolve_api_key_principal",
                new=AsyncMock(return_value=principal),
            ),
            patch("cptr.services.control_auth.require_control_action_memory", new=gate),
        ):
            user_id = await authenticate_control_request(request, "coding:write")
        self.assertEqual(user_id, "user-1")
        gate.assert_awaited_once_with(request, user_id="user-1", required_scope="coding:write")

    async def test_read_scope_does_not_invoke_memory_gate(self):
        request = SimpleNamespace(
            headers={"Authorization": "Bearer secret-token"},
            state=SimpleNamespace(),
        )
        principal = ApiKeyPrincipal(
            user_id="user-1",
            username="tester",
            scopes=frozenset({"coding:read"}),
        )
        gate = AsyncMock()
        with (
            patch("cptr.services.control_auth._hash_key", return_value="hash"),
            patch(
                "cptr.services.control_auth.resolve_api_key_principal",
                new=AsyncMock(return_value=principal),
            ),
            patch("cptr.services.control_auth.require_control_action_memory", new=gate),
        ):
            await authenticate_control_request(request, "coding:read")
        gate.assert_awaited_once_with(request, user_id="user-1", required_scope="coding:read")

    async def test_memory_gate_failure_is_distinct_from_authentication_failure(self):
        request = SimpleNamespace(
            headers={"Authorization": "Bearer secret-token"},
            state=SimpleNamespace(),
        )
        principal = ApiKeyPrincipal(
            user_id="user-1",
            username="tester",
            scopes=frozenset({"command:execute"}),
        )
        with (
            patch("cptr.services.control_auth._hash_key", return_value="hash"),
            patch(
                "cptr.services.control_auth.resolve_api_key_principal",
                new=AsyncMock(return_value=principal),
            ),
            patch(
                "cptr.services.control_auth.require_control_action_memory",
                new=AsyncMock(side_effect=MemoryUnavailableError("blocked")),
            ),
            self.assertRaises(ControlMemoryUnavailable),
        ):
            await authenticate_control_request(request, "command:execute")

    async def test_missing_scope_is_rejected(self):
        request = SimpleNamespace(
            headers={"Authorization": "Bearer secret-token"},
            state=SimpleNamespace(),
        )
        principal = ApiKeyPrincipal(
            user_id="user-1",
            username="tester",
            scopes=frozenset({"workspace:read"}),
        )
        with (
            patch("cptr.services.control_auth._hash_key", return_value="hash"),
            patch(
                "cptr.services.control_auth.resolve_api_key_principal",
                new=AsyncMock(return_value=principal),
            ),
            self.assertRaises(PermissionError),
        ):
            await authenticate_control_request(request, "task:write")

    async def test_invalid_token_is_rejected_without_user_lookup(self):
        request = SimpleNamespace(
            headers={"Authorization": "Bearer invalid"},
            state=SimpleNamespace(),
        )
        with (
            patch(
                "cptr.services.control_auth.resolve_api_key_principal",
                new=AsyncMock(return_value=None),
            ),
            self.assertRaises(PermissionError),
        ):
            await authenticate_control_request(request, "workspace:read")


if __name__ == "__main__":
    unittest.main()
