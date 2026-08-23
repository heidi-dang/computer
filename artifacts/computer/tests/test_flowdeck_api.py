import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from cptr.flowdeck.coordinator import CoordinatorResult
from cptr.flowdeck.durable import RunStatus
from cptr.routers.flowdeck import (
    OrchestrationRequest,
    create_orchestration,
)


def request_with_key(key: str = "request-1234") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/flowdeck/orchestrations",
            "headers": [(b"idempotency-key", key.encode())],
            "client": ("127.0.0.1", 0),
            "server": ("internal", 0),
            "scheme": "http",
            "state": {},
        }
    )


class FlowDeckProductionApiTests(unittest.IsolatedAsyncioTestCase):
    def body(self):
        return OrchestrationRequest(workspace="/owned", objective="review the repository")

    async def test_disabled_gate_has_zero_orchestration_side_effects(self):
        with (
            patch.dict(os.environ, {"CPTR_FLOWDECK_ENABLED": "false"}, clear=False),
            patch("cptr.routers.flowdeck.DurableFlowDeck") as store,
            patch(
                "cptr.routers.flowdeck.run_heidi_coordinator",
                new=AsyncMock(),
            ) as coordinator,
            patch(
                "cptr.routers.flowdeck._authenticate",
                new=AsyncMock(return_value="owner"),
            ),
        ):
            with self.assertRaises(HTTPException) as error:
                await create_orchestration(request_with_key(), self.body())
        self.assertEqual(error.exception.status_code, 404)
        store.assert_not_called()
        coordinator.assert_not_awaited()

    async def test_contract_rejects_client_authority_fields(self):
        with self.assertRaises(ValidationError):
            OrchestrationRequest(
                workspace="/owned",
                objective="inspect",
                model="client-selected-model",
                user_id="forged-user",
                budget=999999,
            )

    async def test_authenticated_route_uses_server_model_and_only_coordinator(self):
        fake_store = SimpleNamespace(session_factory=object())
        target = SimpleNamespace(
            kind="api",
            runtime_model="server-selected-model",
            connection={"provider": "server"},
        )
        result = CoordinatorResult("succeeded", "run-1", (), ())
        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_COORDINATOR_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                },
                clear=False,
            ),
            patch(
                "cptr.routers.flowdeck._authenticate",
                new=AsyncMock(return_value="owner"),
            ),
            patch(
                "cptr.routers.flowdeck.resolve_gateway_workspace",
                new=AsyncMock(return_value="/owned"),
            ),
            patch(
                "cptr.routers.flowdeck._resolve_model",
                new=AsyncMock(return_value=(target, "ignored")),
            ),
            patch(
                "cptr.routers.flowdeck.DurableFlowDeck",
                return_value=fake_store,
            ),
            patch(
                "cptr.routers.flowdeck.run_heidi_coordinator",
                new=AsyncMock(return_value=result),
            ) as coordinator,
        ):
            request = request_with_key()
            request.scope["app"] = SimpleNamespace(state=SimpleNamespace())
            response = await create_orchestration(request, self.body())
        self.assertEqual(response["run_id"], "run-1")
        coordinator.assert_awaited_once()
        coordinator_request = coordinator.await_args.args[0]
        self.assertEqual(coordinator_request.model, "server-selected-model")
        self.assertEqual(coordinator_request.connection, {"provider": "server"})

    def test_cancelled_is_not_success(self):
        self.assertNotEqual(RunStatus.CANCELLED.value.lower(), "succeeded")