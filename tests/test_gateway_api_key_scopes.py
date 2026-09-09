import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from cptr.routers.gateway import CreateApiKeyRequest, create_api_key
from cptr.routers.gateway_extended import UpdateKeyRequest, update_api_key


CAPABILITY_SCOPES = {"capability:read", "capability:write", "capability:execute"}


class ApiKeyCapabilityScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_key_issuer_can_explicitly_enable_capability_os_without_changing_defaults(self):
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), cookies={})
        saved = AsyncMock()
        with (
            patch(
                "cptr.utils.config.check_access",
                return_value=SimpleNamespace(user_id="user_1"),
            ),
            patch("cptr.routers.gateway._get_api_keys", new=AsyncMock(return_value=[])),
            patch("cptr.routers.gateway._save_api_keys", new=saved),
        ):
            await create_api_key(
                request,
                CreateApiKeyRequest(name="capability", capability_os=True),
            )

        scopes = set(saved.await_args.args[0][0]["scopes"])
        self.assertIn("coding:write", scopes)
        self.assertTrue(CAPABILITY_SCOPES <= scopes)

    async def test_existing_key_can_enable_capability_os_without_rotating_or_losing_scopes(self):
        original_hash = "hash-stays-stable"
        key = {
            "id": "key-1",
            "key_hash": original_hash,
            "user_id": "user_1",
            "name": "plugin",
            "scopes": ["coding:read", "command:external"],
            "created_at": 1,
        }
        save = AsyncMock()
        with (
            patch("cptr.routers.gateway_extended.require_admin"),
            patch(
                "cptr.routers.gateway_extended.list_api_keys",
                new=AsyncMock(return_value=[key]),
            ),
            patch("cptr.routers.gateway_extended.save_api_keys", new=save),
        ):
            result = await update_api_key(
                SimpleNamespace(),
                "key-1",
                UpdateKeyRequest(capability_os=True),
            )

        saved_key = save.await_args.args[0][0]
        self.assertEqual(saved_key["key_hash"], original_hash)
        self.assertIn("command:external", saved_key["scopes"])
        self.assertTrue(CAPABILITY_SCOPES <= set(saved_key["scopes"]))
        self.assertTrue(CAPABILITY_SCOPES <= set(result["scopes"]))

    async def test_existing_key_can_disable_capability_os_without_losing_other_optional_scopes(
        self,
    ):
        key = {
            "id": "key-1",
            "key_hash": "hash-stays-stable",
            "user_id": "user_1",
            "name": "plugin",
            "scopes": [
                "coding:read",
                "command:external",
                "capability:read",
                "capability:write",
                "capability:execute",
            ],
            "created_at": 1,
        }
        save = AsyncMock()
        with (
            patch("cptr.routers.gateway_extended.require_admin"),
            patch(
                "cptr.routers.gateway_extended.list_api_keys",
                new=AsyncMock(return_value=[key]),
            ),
            patch("cptr.routers.gateway_extended.save_api_keys", new=save),
        ):
            await update_api_key(
                SimpleNamespace(),
                "key-1",
                UpdateKeyRequest(capability_os=False),
            )

        saved_scopes = set(save.await_args.args[0][0]["scopes"])
        self.assertIn("coding:read", saved_scopes)
        self.assertIn("command:external", saved_scopes)
        self.assertTrue(CAPABILITY_SCOPES.isdisjoint(saved_scopes))

    async def test_scope_update_rejects_unknown_or_empty_scope_sets(self):
        key = {
            "id": "key-1",
            "key_hash": "hash-stays-stable",
            "user_id": "user_1",
            "name": "plugin",
            "scopes": ["coding:read"],
            "created_at": 1,
        }
        for scopes in (["coding:read", "workspace:root"], []):
            with self.subTest(scopes=scopes):
                save = AsyncMock()
                with (
                    patch("cptr.routers.gateway_extended.require_admin"),
                    patch(
                        "cptr.routers.gateway_extended.list_api_keys",
                        new=AsyncMock(return_value=[dict(key)]),
                    ),
                    patch("cptr.routers.gateway_extended.save_api_keys", new=save),
                    self.assertRaises(HTTPException) as rejected,
                ):
                    await update_api_key(
                        SimpleNamespace(),
                        "key-1",
                        UpdateKeyRequest(scopes=scopes),
                    )

                self.assertEqual(rejected.exception.status_code, 422)
                save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
