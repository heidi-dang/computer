import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.control_auth import authenticate_control_request


class ControlAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_bearer_token_is_accepted(self):
        request = SimpleNamespace(
            headers={"Authorization": "Bearer secret-token"},
            state=SimpleNamespace(),
        )
        key = {
            "key_hash": "hash",
            "user_id": "user-1",
            "scopes": ["workspace:read", "task:read"],
        }
        with (
            patch("cptr.services.control_auth._hash_key", return_value="hash"),
            patch("cptr.services.control_auth._get_api_keys", new=AsyncMock(return_value=[key])),
            patch(
                "cptr.services.control_auth.Auth.get_by_user_id",
                new=AsyncMock(return_value=SimpleNamespace(username="user-one")),
            ),
        ):
            user_id = await authenticate_control_request(request, "workspace:read")

        self.assertEqual(user_id, "user-1")
        self.assertEqual(request.state.control_scopes, {"workspace:read", "task:read"})

    async def test_missing_scope_is_rejected(self):
        request = SimpleNamespace(
            headers={"Authorization": "Bearer secret-token"},
            state=SimpleNamespace(),
        )
        key = {"key_hash": "hash", "user_id": "user-1", "scopes": ["workspace:read"]}
        with (
            patch("cptr.services.control_auth._hash_key", return_value="hash"),
            patch("cptr.services.control_auth._get_api_keys", new=AsyncMock(return_value=[key])),
            self.assertRaises(PermissionError),
        ):
            await authenticate_control_request(request, "task:write")


if __name__ == "__main__":
    unittest.main()
