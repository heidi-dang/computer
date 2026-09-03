import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from cptr.routers.browser_device import (
    PairingApproveBody,
    PairingClaimBody,
    PairingRequestBody,
    SendCommandBody,
    TransferLeaseBody,
    approve_pairing,
    browser_device_control_socket,
    claim_pairing,
    request_pairing,
    router,
    send_browser_command,
    transfer_browser_lease,
)
from cptr.services.browser_devices import PairingRequest


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if not self.messages:
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        value = self.messages.pop(0)
        return json.dumps(value)

    async def receive_json(self):
        if not self.messages:
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        return self.messages.pop(0)

    async def send_json(self, value):
        self.sent.append(value)

    async def close(self, code, reason):
        self.closed.append((code, reason))


class BrowserDeviceRouterTests(unittest.IsolatedAsyncioTestCase):
    def test_router_registers_expected_contract(self):
        paths = {route.path for route in router.routes if hasattr(route, "path")}
        self.assertIn("/api/browser-device/v1/pairing/request", paths)
        self.assertIn("/api/browser-device/v1/pairing/approve", paths)
        self.assertIn("/api/browser-device/v1/pairing/claim", paths)
        self.assertIn("/api/browser-device/v1/devices", paths)
        self.assertIn("/api/browser-device/v1/connect/control", paths)
        self.assertIn("/api/browser-device/v1/sessions/{session_id}/command", paths)

    async def test_pairing_request_returns_claim_secret_only_to_extension(self):
        with patch(
            "cptr.routers.browser_device.browser_device_store.request_pairing",
            new=AsyncMock(
                return_value=PairingRequest(
                    pairing_id="pair_1",
                    code="123456",
                    claim_secret="claim-secret-value",
                    expires_at=123,
                )
            ),
        ):
            result = await request_pairing(PairingRequestBody(device_name="Heidi Chrome"))
        self.assertEqual(result["pairing_id"], "pair_1")
        self.assertEqual(result["code"], "123456")
        self.assertEqual(result["claim_secret"], "claim-secret-value")

    async def test_approval_requires_authenticated_owner(self):
        request = SimpleNamespace()
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch(
                "cptr.routers.browser_device.browser_device_store.approve_pairing",
                new=AsyncMock(return_value=True),
            ) as approve,
        ):
            result = await approve_pairing(
                request,
                PairingApproveBody(pairing_id="pair_1", code="123456"),
            )
        self.assertTrue(result["approved"])
        approve.assert_awaited_once_with(user_id="user_1", pairing_id="pair_1", code="123456")

    async def test_claim_returns_raw_device_credential_once(self):
        device = SimpleNamespace(id="bdv_1", name="Heidi Chrome", credential_version=1)
        with patch(
            "cptr.routers.browser_device.browser_device_store.claim_pairing",
            new=AsyncMock(return_value=(device, "device-secret")),
        ):
            result = await claim_pairing(
                PairingClaimBody(pairing_id="pair_1", claim_secret="x" * 32)
            )
        self.assertEqual(result["device_credential"], "device-secret")
        self.assertNotIn("credential_hash", result)

    async def test_lease_transfer_rejects_cross_owner_session(self):
        request = SimpleNamespace()
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch(
                "cptr.routers.browser_device.browser_device_store.get_session",
                new=AsyncMock(return_value=None),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            await transfer_browser_lease(
                request,
                "brs_other",
                TransferLeaseBody(
                    expected_epoch=1,
                    expected_owner="agent",
                    new_owner="human",
                ),
            )
        self.assertEqual(raised.exception.status_code, 404)

    async def test_agent_command_requires_current_lease_epoch_before_delivery(self):
        request = SimpleNamespace()
        session = SimpleNamespace(device_id="bdv_1")
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch(
                "cptr.routers.browser_device.browser_device_store.get_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "cptr.routers.browser_device.browser_device_store.assert_mutation",
                new=AsyncMock(),
            ) as assert_mutation,
            patch(
                "cptr.routers.browser_device.browser_device_connections.send_control",
                new=AsyncMock(return_value=True),
            ) as send,
        ):
            result = await send_browser_command(
                request,
                "brs_1",
                SendCommandBody(
                    command_id="cmd_1",
                    action="click",
                    expected_epoch=9,
                    payload={"ref": "ref_1"},
                ),
            )
        self.assertTrue(result["accepted"])
        assert_mutation.assert_awaited_once_with(
            session_id="brs_1", actor="agent", expected_epoch=9
        )
        self.assertEqual(send.await_args.kwargs["message"]["expected_epoch"], 9)

    async def test_websocket_authenticates_then_replays_from_cursor(self):
        socket = FakeWebSocket(
            [
                {
                    "protocol_version": 1,
                    "type": "device.authenticate",
                    "device_id": "bdv_1",
                    "device_credential": "secret",
                    "resume_from": 7,
                }
            ]
        )
        with (
            patch(
                "cptr.routers.browser_device.browser_device_store.authenticate_device",
                new=AsyncMock(return_value=SimpleNamespace(id="bdv_1")),
            ),
            patch(
                "cptr.routers.browser_device.browser_device_connections.attach",
                new=AsyncMock(),
            ),
            patch(
                "cptr.routers.browser_device.browser_device_connections.detach",
                new=AsyncMock(),
            ),
            patch(
                "cptr.routers.browser_device.browser_device_store.replay_device_events",
                new=AsyncMock(
                    return_value=[
                        {
                            "event_id": "evt_8",
                            "device_id": "bdv_1",
                            "sequence": 8,
                            "type": "browser.command",
                            "timestamp_ms": 1,
                            "payload": {"command_id": "cmd_1"},
                        }
                    ]
                ),
            ) as replay,
        ):
            await browser_device_control_socket(socket)
        self.assertTrue(socket.accepted)
        self.assertEqual(socket.sent[0]["type"], "device.authenticated")
        self.assertEqual(socket.sent[1]["sequence"], 8)
        replay.assert_awaited_once_with(device_id="bdv_1", after_sequence=7)

    async def test_websocket_rejects_bad_device_without_replay(self):
        socket = FakeWebSocket(
            [
                {
                    "protocol_version": 1,
                    "type": "device.authenticate",
                    "device_id": "bdv_1",
                    "device_credential": "bad-secret",
                    "resume_from": 0,
                }
            ]
        )
        with (
            patch(
                "cptr.routers.browser_device.browser_device_store.authenticate_device",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "cptr.routers.browser_device.browser_device_connections.attach",
                new=AsyncMock(),
            ),
            patch(
                "cptr.routers.browser_device.browser_device_connections.detach",
                new=AsyncMock(),
            ),
            patch(
                "cptr.routers.browser_device.browser_device_store.replay_device_events",
                new=AsyncMock(),
            ) as replay,
        ):
            await browser_device_control_socket(socket)
        self.assertEqual(socket.closed, [(1008, "device authentication failed")])
        replay.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
