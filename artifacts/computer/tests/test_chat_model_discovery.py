import types
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from cptr.routers.chat import (
    _availability_for_discovery_failure,
    _get_connection_model_metadata,
    _resolve_connection,
    _verify_exact_model,
)


class ChatModelDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = {
            "id": "managed",
            "provider": "openai",
            "prefix_id": "chatgpt",
            "base_url": "http://provider/v1",
            "data": {"models": ["gpt-5.6-terra"]},
        }

    def test_http_405_is_unverified_not_available(self):
        self.assertEqual(_availability_for_discovery_failure("Provider returned HTTP 405"), "unverified")
        self.assertEqual(_availability_for_discovery_failure("Provider returned HTTP 401"), "unavailable")

    async def test_discovery_405_keeps_configured_model_visible_but_unverified(self):
        state = types.SimpleNamespace()
        with patch(
            "cptr.routers.chat._get_connections",
            new=AsyncMock(return_value=[self.connection]),
        ), patch(
            "cptr.routers.chat._fetch_provider_model_records_with_status",
            new=AsyncMock(return_value=(None, "Provider returned HTTP 405")),
        ), patch(
            "cptr.utils.connection_credentials.connection_api_key",
            return_value="managed-runtime-key",
        ), patch(
            "cptr.routers.chat._verify_exact_model",
            new=AsyncMock(return_value=("unverified", "Provider returned HTTP 405")),
        ):
            metadata = await _get_connection_model_metadata(self.connection, state, force_refresh=True)

        self.assertEqual(metadata["gpt-5.6-terra"]["availability"], "unverified")
        self.assertIn("405", metadata["gpt-5.6-terra"]["availability_reason"])

    async def test_unverified_model_cannot_be_resolved(self):
        state = types.SimpleNamespace()
        with patch(
            "cptr.routers.chat._get_connections",
            new=AsyncMock(return_value=[self.connection]),
        ), patch(
            "cptr.routers.chat._fetch_provider_model_records_with_status",
            new=AsyncMock(return_value=(None, "Provider returned HTTP 405")),
        ), patch(
            "cptr.utils.connection_credentials.connection_api_key",
            return_value="managed-runtime-key",
        ), patch(
            "cptr.routers.chat._verify_exact_model",
            new=AsyncMock(return_value=("unverified", "Provider returned HTTP 405")),
        ):
            with self.assertRaises(HTTPException) as raised:
                await _resolve_connection("chatgpt/gpt-5.6-terra", state)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("not available", str(raised.exception.detail))

    async def test_verified_discovery_allows_selected_model(self):
        state = types.SimpleNamespace()
        with patch(
            "cptr.routers.chat._get_connections",
            new=AsyncMock(return_value=[self.connection]),
        ), patch(
            "cptr.routers.chat._fetch_provider_model_records_with_status",
            new=AsyncMock(
                return_value=(
                    [{"id": "gpt-5.6-terra", "pricing": {"input_price_per_1m": 1.0}}],
                    None,
                )
            ),
        ), patch(
            "cptr.utils.connection_credentials.connection_api_key",
            return_value="managed-runtime-key",
        ), patch(
            "cptr.routers.chat._verify_exact_model",
            new=AsyncMock(
                return_value=("available", "Confirmed by exact-model provider verification")
            ),
        ):
            connection, model = await _resolve_connection("chatgpt/gpt-5.6-terra", state)

        self.assertEqual(connection["id"], "managed")
        self.assertEqual(model, "gpt-5.6-terra")
        self.assertEqual(
            state.MODEL_METADATA["managed"]["models"]["gpt-5.6-terra"]["input_price_per_1m"], 1.0
        )

    async def test_exact_verification_auth_failure_is_unavailable(self):
        class Response:
            status_code = 401

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def get(self, *_args, **_kwargs):
                return Response()

        with patch("httpx.AsyncClient", return_value=Client()), patch(
            "cptr.utils.connection_credentials.connection_api_key",
            return_value="runtime-key",
        ):
            self.assertEqual(
                await _verify_exact_model(self.connection, "gpt-5.6-terra"),
                ("unavailable", "Provider returned HTTP 401"),
            )

    async def test_exact_verification_405_is_unverified(self):
        class Response:
            status_code = 405

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def get(self, *_args, **_kwargs):
                return Response()

        with patch("httpx.AsyncClient", return_value=Client()), patch(
            "cptr.utils.connection_credentials.connection_api_key",
            return_value="runtime-key",
        ):
            availability, reason = await _verify_exact_model(
                self.connection, "gpt-5.6-terra"
            )
        self.assertEqual(availability, "unverified")
        self.assertIn("405", reason)

    async def test_cache_expires_and_invalidates_when_provider_config_changes(self):
        state = types.SimpleNamespace()
        verifier = AsyncMock(return_value=("available", "verified"))
        with patch(
            "cptr.routers.chat._fetch_provider_model_records_with_status",
            new=AsyncMock(return_value=(None, "Provider returned HTTP 405")),
        ), patch(
            "cptr.utils.connection_credentials.connection_api_key",
            return_value="runtime-key",
        ), patch("cptr.routers.chat._verify_exact_model", new=verifier):
            first = await _get_connection_model_metadata(self.connection, state)
            second = await _get_connection_model_metadata(self.connection, state)
            changed = {**self.connection, "base_url": "http://provider/v2"}
            third = await _get_connection_model_metadata(changed, state)

        self.assertEqual(first["gpt-5.6-terra"]["availability"], "available")
        self.assertIs(second, first)
        self.assertEqual(third["gpt-5.6-terra"]["availability"], "available")
        self.assertEqual(verifier.await_count, 2)


if __name__ == "__main__":
    unittest.main()