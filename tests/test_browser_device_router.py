import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from cptr.routers.browser_device import (
    OpenSessionBody,
    EvaluateApprovalBody,
    PairingApproveBody,
    PairingClaimBody,
    PairingRequestBody,
    HumanInputBody,
    ReturnToAgentBody,
    StreamConfigureBody,
    SendCommandBody,
    TransferLeaseBody,
    approve_pairing,
    approve_browser_evaluate,
    browser_device_control_socket,
    browser_device_visual_socket,
    claim_pairing,
    request_pairing,
    router,
    send_browser_command,
    send_browser_human_input,
    return_browser_to_agent,
    configure_browser_stream,
    get_browser_frame,
    open_browser_session,
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
        self.assertIn("/api/browser-device/v1/connect/visual", paths)
        self.assertIn("/api/browser-device/v1/sessions/{session_id}/command", paths)
        self.assertIn("/api/browser-device/v1/sessions/{session_id}/human-input", paths)
        self.assertIn("/api/browser-device/v1/sessions/{session_id}/return-to-agent", paths)
        self.assertIn("/api/browser-device/v1/sessions/{session_id}/stream-config", paths)
        self.assertIn("/api/browser-device/v1/sessions/{session_id}/evaluate-approval", paths)

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

    async def test_open_session_acquires_agent_epoch_and_confirms_attach(self):
        request = SimpleNamespace()
        session = SimpleNamespace(id="brs_1", device_id="bdv_1", tab_id=7, surface_id="surf_1")
        acquired = {"device_id": "bdv_1", "tab_id": 7, "session_id": "brs_1", "owner": "agent", "epoch": 9, "snapshot_id": None, "state": "AGENT_CONTROL"}
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.browser_device.browser_device_store.open_session", new=AsyncMock(return_value=session)),
            patch("cptr.routers.browser_device.browser_device_store.session_lease", new=AsyncMock(return_value={"epoch": 8, "owner": "none"})),
            patch("cptr.routers.browser_device.browser_device_store.transfer_lease", new=AsyncMock(return_value=acquired)) as transfer,
            patch("cptr.routers.browser_device.browser_device_store.append_device_event", new=AsyncMock(return_value=SimpleNamespace(sequence=18))),
            patch("cptr.routers.browser_device.browser_device_connections.send_control", new=AsyncMock(return_value=True)) as send,
            patch("cptr.routers.browser_device.browser_command_results.reserve", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_command_results.wait", new=AsyncMock(return_value={"type": "browser.command.completed", "payload": {"tab": {"id": 7}, "lease": acquired}})),
        ):
            result = await open_browser_session(request, OpenSessionBody(device_id="bdv_1", tab_id=7, surface_id="surf_1"))
        self.assertEqual(result["state"], "AGENT_CONTROL")
        self.assertEqual(result["lease"]["epoch"], 9)
        transfer.assert_awaited_once_with(session_id="brs_1", expected_epoch=8, expected_owner="none", new_owner="agent")
        message = send.await_args.kwargs["message"]
        self.assertEqual(message["payload"]["action"], "attach")
        self.assertEqual(message["payload"]["expected_epoch"], 9)
        self.assertEqual(message["payload"]["args"], {"tab_id": 7})

    async def test_open_session_does_not_report_ready_when_attach_fails(self):
        request = SimpleNamespace()
        session = SimpleNamespace(id="brs_1", device_id="bdv_1", tab_id=7, surface_id="surf_1")
        acquired = {"device_id": "bdv_1", "tab_id": 7, "session_id": "brs_1", "owner": "agent", "epoch": 9, "snapshot_id": None, "state": "AGENT_CONTROL"}
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.browser_device.browser_device_store.open_session", new=AsyncMock(return_value=session)),
            patch("cptr.routers.browser_device.browser_device_store.session_lease", new=AsyncMock(return_value={"epoch": 8, "owner": "none"})),
            patch("cptr.routers.browser_device.browser_device_store.transfer_lease", new=AsyncMock(return_value=acquired)),
            patch("cptr.routers.browser_device.browser_device_store.append_device_event", new=AsyncMock(return_value=SimpleNamespace(sequence=18))),
            patch("cptr.routers.browser_device.browser_device_connections.send_control", new=AsyncMock(return_value=True)),
            patch("cptr.routers.browser_device.browser_command_results.reserve", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_command_results.wait", new=AsyncMock(return_value={"type": "browser.command.failed", "payload": {"error": "debugger busy"}})),
            patch("cptr.routers.browser_device.browser_device_store.abort_session_bootstrap", new=AsyncMock()) as abort,
            self.assertRaises(HTTPException) as raised,
        ):
            await open_browser_session(request, OpenSessionBody(device_id="bdv_1", tab_id=7, surface_id="surf_1"))
        self.assertEqual(raised.exception.status_code, 409)
        abort.assert_awaited_once_with(session_id="brs_1", expected_epoch=9)

    async def test_evaluate_requires_one_time_exact_approval_before_dispatch(self):
        request = SimpleNamespace()
        session = SimpleNamespace(device_id="bdv_1", surface_id="surf_1", state="AGENT_CONTROL")
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.browser_device.browser_device_store.get_session", new=AsyncMock(return_value=session)),
            patch("cptr.routers.browser_device.browser_device_store.assert_mutation", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_evaluate_approvals.issue", return_value=SimpleNamespace(token="approval_1")),
        ):
            approval = await approve_browser_evaluate(request, "brs_1", EvaluateApprovalBody(expression="document.title"))
        self.assertEqual(approval["approval_token"], "approval_1")

        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.browser_device.browser_device_store.get_session", new=AsyncMock(return_value=session)),
            patch("cptr.routers.browser_device.browser_device_store.assert_mutation", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_evaluate_approvals.consume", return_value=False) as consume,
            self.assertRaises(HTTPException) as raised,
        ):
            await send_browser_command(
                request,
                "brs_1",
                SendCommandBody(
                    command_id="cmd_eval",
                    action="evaluate",
                    expected_epoch=4,
                    payload={"expression": "document.title", "approval_token": "approval_1"},
                ),
            )
        self.assertEqual(raised.exception.status_code, 403)
        consume.assert_called_once_with(token="approval_1", user_id="user_1", session_id="brs_1", expression="document.title")

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

    async def test_lease_transfer_notifies_extension_with_authoritative_epoch(self):
        request = SimpleNamespace()
        session = SimpleNamespace(device_id="bdv_1", surface_id="surf_1")
        result = {
            "device_id": "bdv_1",
            "tab_id": 7,
            "session_id": "brs_1",
            "owner": "human",
            "epoch": 10,
            "snapshot_id": "snap_9",
            "state": "HUMAN_CONTROL",
        }
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.browser_device.browser_device_store.get_session", new=AsyncMock(return_value=session)),
            patch("cptr.routers.browser_device.browser_device_store.transfer_lease", new=AsyncMock(return_value=result)),
            patch(
                "cptr.routers.browser_device.browser_device_store.append_device_event",
                new=AsyncMock(return_value=SimpleNamespace(sequence=22)),
            ),
            patch(
                "cptr.routers.browser_device.browser_device_connections.send_control",
                new=AsyncMock(return_value=True),
            ) as send,
        ):
            response = await transfer_browser_lease(
                request,
                "brs_1",
                TransferLeaseBody(expected_epoch=9, expected_owner="agent", new_owner="human"),
            )
        self.assertEqual(response["epoch"], 10)
        message = send.await_args.kwargs["message"]
        self.assertEqual(message["type"], "browser.handoff.accepted")
        self.assertEqual(message["sequence"], 22)
        self.assertEqual(message["mode"], "HUMAN_CONTROL")
        self.assertEqual(message["payload"]["owner"], "human")
        self.assertEqual(message["payload"]["epoch"], 10)

    async def test_frame_read_is_owner_scoped_and_no_store(self):
        request = SimpleNamespace()
        session = SimpleNamespace(device_id="bdv_1")
        frame = SimpleNamespace(
            session_id="brs_1",
            data=b"jpeg-bytes",
            mime_type="image/jpeg",
            frame_id="frm_1",
            width=640,
            height=480,
            created_at_ms=123,
        )
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.browser_device.browser_device_store.get_session", new=AsyncMock(return_value=session)),
            patch("cptr.routers.browser_device.browser_visual_frames.wait_next", new=AsyncMock(return_value=frame)),
        ):
            response = await get_browser_frame(request, "brs_1", "frm_0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"jpeg-bytes")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-cptr-frame-id"], "frm_1")

        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_2")),
            patch("cptr.routers.browser_device.browser_device_store.get_session", new=AsyncMock(return_value=None)),
            self.assertRaises(HTTPException) as raised,
        ):
            await get_browser_frame(request, "brs_1", None)
        self.assertEqual(raised.exception.status_code, 404)

    async def test_stream_configuration_is_owner_scoped_and_forwarded(self):
        request = SimpleNamespace()
        session = SimpleNamespace(device_id="bdv_1", surface_id="surf_1", state="HUMAN_CONTROL")
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.browser_device.browser_device_store.get_session", new=AsyncMock(return_value=session)),
            patch("cptr.routers.browser_device.browser_device_store.append_device_event", new=AsyncMock(return_value=SimpleNamespace(sequence=35))),
            patch("cptr.routers.browser_device.browser_device_connections.send_control", new=AsyncMock(return_value=True)) as send,
        ):
            result = await configure_browser_stream(
                request,
                "brs_1",
                StreamConfigureBody(visible=False, max_fps=0, max_width=960, quality=55),
            )
        self.assertTrue(result["configured"])
        message = send.await_args.kwargs["message"]
        self.assertEqual(message["type"], "browser.stream.configure")
        self.assertEqual(message["mode"], "HUMAN_CONTROL")
        self.assertFalse(message["payload"]["visible"])
        self.assertEqual(message["payload"]["max_fps"], 0)

    async def test_return_to_agent_requires_fresh_snapshot_before_transfer(self):
        request = SimpleNamespace()
        session = SimpleNamespace(device_id="bdv_1", surface_id="surf_1")
        result = {
            "device_id": "bdv_1", "tab_id": 7, "session_id": "brs_1",
            "owner": "agent", "epoch": 11, "snapshot_id": "snap_fresh", "state": "AGENT_CONTROL",
        }
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.browser_device.browser_device_store.get_session", new=AsyncMock(return_value=session)),
            patch("cptr.routers.browser_device.browser_device_store.assert_mutation", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_device_store.append_device_event", new=AsyncMock(side_effect=[SimpleNamespace(sequence=40), SimpleNamespace(sequence=41)])),
            patch("cptr.routers.browser_device.browser_device_connections.send_control", new=AsyncMock(return_value=True)) as send,
            patch("cptr.routers.browser_device.browser_command_results.reserve", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_command_results.wait", new=AsyncMock(return_value={"type": "browser.command.completed", "payload": {"snapshot_id": "snap_fresh"}})),
            patch("cptr.routers.browser_device.browser_device_store.transfer_lease", new=AsyncMock(return_value=result)) as transfer,
        ):
            response = await return_browser_to_agent(request, "brs_1", ReturnToAgentBody(expected_epoch=10))
        self.assertEqual(response["owner"], "agent")
        transfer.assert_awaited_once_with(session_id="brs_1", expected_epoch=10, expected_owner="human", new_owner="agent", fresh_snapshot_id="snap_fresh")
        messages = [call.kwargs["message"] for call in send.await_args_list]
        self.assertEqual(messages[0]["type"], "browser.handoff.prepare_return")
        self.assertEqual(messages[0]["payload"]["expected_epoch"], 10)
        self.assertEqual(messages[1]["type"], "browser.handoff.returned")
        self.assertEqual(messages[1]["payload"]["snapshot_id"], "snap_fresh")
        self.assertEqual(messages[1]["payload"]["epoch"], 11)

    async def test_return_to_agent_does_not_transfer_when_snapshot_fails(self):
        request = SimpleNamespace()
        session = SimpleNamespace(device_id="bdv_1", surface_id="surf_1")
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.browser_device.browser_device_store.get_session", new=AsyncMock(return_value=session)),
            patch("cptr.routers.browser_device.browser_device_store.assert_mutation", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_device_store.append_device_event", new=AsyncMock(return_value=SimpleNamespace(sequence=40))),
            patch("cptr.routers.browser_device.browser_device_connections.send_control", new=AsyncMock(return_value=True)),
            patch("cptr.routers.browser_device.browser_command_results.reserve", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_command_results.wait", new=AsyncMock(return_value={"type": "browser.command.failed", "payload": {"error": "snapshot failed"}})),
            patch("cptr.routers.browser_device.browser_device_store.transfer_lease", new=AsyncMock()) as transfer,
            self.assertRaises(HTTPException) as raised,
        ):
            await return_browser_to_agent(request, "brs_1", ReturnToAgentBody(expected_epoch=10))
        self.assertEqual(raised.exception.status_code, 409)
        transfer.assert_not_awaited()

    async def test_human_input_requires_human_epoch_and_does_not_persist_text(self):
        request = SimpleNamespace()
        session = SimpleNamespace(device_id="bdv_1", surface_id="surf_1")
        with (
            patch("cptr.routers.browser_device._control_user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.browser_device.browser_device_store.get_session", new=AsyncMock(return_value=session)),
            patch("cptr.routers.browser_device.browser_device_store.assert_mutation", new=AsyncMock()) as assert_mutation,
            patch("cptr.routers.browser_device.browser_device_store.append_device_event", new=AsyncMock(return_value=SimpleNamespace(sequence=31))) as append_event,
            patch("cptr.routers.browser_device.browser_device_connections.send_control", new=AsyncMock(return_value=True)) as send,
            patch("cptr.routers.browser_device.browser_command_results.reserve", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_command_results.wait", new=AsyncMock(return_value={"type": "browser.command.completed", "payload": {"ok": True}})),
        ):
            result = await send_browser_human_input(
                request,
                "brs_1",
                HumanInputBody(
                    command_id="human_1",
                    expected_epoch=10,
                    input_type="text_input",
                    text="super-secret-password",
                    sensitive=True,
                ),
            )
        self.assertTrue(result["accepted"])
        assert_mutation.assert_awaited_once_with(session_id="brs_1", actor="human", expected_epoch=10)
        persisted = append_event.await_args.kwargs["payload"]
        self.assertNotIn("text", persisted)
        self.assertNotIn("super-secret-password", json.dumps(persisted))
        message = send.await_args.kwargs["message"]
        self.assertEqual(message["type"], "browser.human.input")
        self.assertEqual(message["mode"], "HUMAN_CONTROL")
        self.assertEqual(message["payload"]["expected_epoch"], 10)
        self.assertEqual(message["payload"]["text"], "super-secret-password")
        self.assertTrue(message["payload"]["sensitive"])

    async def test_agent_command_requires_current_lease_epoch_before_delivery(self):
        request = SimpleNamespace()
        session = SimpleNamespace(device_id="bdv_1", surface_id="surf_1", state="AGENT_CONTROL")
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
                "cptr.routers.browser_device.browser_device_store.session_lease",
                new=AsyncMock(return_value={"owner": "agent", "epoch": 9}),
            ),
            patch(
                "cptr.routers.browser_device.browser_device_store.append_device_event",
                new=AsyncMock(return_value=SimpleNamespace(sequence=12)),
            ),
            patch(
                "cptr.routers.browser_device.browser_device_connections.send_control",
                new=AsyncMock(return_value=True),
            ) as send,
            patch(
                "cptr.routers.browser_device.browser_command_results.reserve",
                new=AsyncMock(),
            ),
            patch(
                "cptr.routers.browser_device.browser_command_results.wait",
                new=AsyncMock(return_value={"type": "browser.command.completed", "command_id": "cmd_1", "payload": {}}),
            ),
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
        message = send.await_args.kwargs["message"]
        self.assertEqual(message["surface_id"], "surf_1")
        self.assertEqual(message["sequence"], 12)
        self.assertEqual(message["mode"], "AGENT_CONTROL")
        self.assertEqual(message["payload"]["expected_epoch"], 9)
        self.assertEqual(message["payload"]["action"], "click")
        self.assertEqual(message["payload"]["args"], {"ref": "ref_1"})
        self.assertEqual(result["result"]["type"], "browser.command.completed")

    async def test_websocket_completes_matching_command_id(self):
        socket = FakeWebSocket(
            [
                {
                    "protocol_version": 1,
                    "type": "device.authenticate",
                    "device_id": "bdv_1",
                    "device_credential": "secret",
                    "resume_from": 0,
                },
                {
                    "protocol_version": 1,
                    "type": "browser.command.completed",
                    "device_id": "bdv_1",
                    "command_id": "cmd_1",
                    "payload": {"ok": True},
                },
            ]
        )
        with (
            patch(
                "cptr.routers.browser_device.browser_device_store.authenticate_device",
                new=AsyncMock(return_value=SimpleNamespace(id="bdv_1")),
            ),
            patch("cptr.routers.browser_device.browser_device_connections.attach", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_device_connections.detach", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_device_store.replay_device_events", new=AsyncMock(return_value=[])),
            patch("cptr.routers.browser_device.browser_device_store.append_device_event", new=AsyncMock()),
            patch("cptr.routers.browser_device.browser_command_results.complete", new=AsyncMock(return_value=True)) as complete,
        ):
            await browser_device_control_socket(socket)
        complete.assert_awaited_once()
        self.assertEqual(complete.await_args.args[0], "cmd_1")

    async def test_visual_websocket_authenticates_and_stores_latest_frame(self):
        socket = FakeWebSocket(
            [
                {
                    "protocol_version": 1,
                    "type": "device.authenticate",
                    "device_id": "bdv_1",
                    "device_credential": "secret",
                    "resume_from": 0,
                },
                {
                    "protocol_version": 1,
                    "type": "browser.frame",
                    "device_id": "bdv_1",
                    "session_id": "brs_1",
                    "frame_id": "frm_1",
                    "mime_type": "image/jpeg",
                    "width": 640,
                    "height": 480,
                    "created_at_ms": 123,
                    "data_base64": "aGVsbG8=",
                },
            ]
        )
        with (
            patch(
                "cptr.routers.browser_device.browser_device_store.authenticate_device",
                new=AsyncMock(return_value=SimpleNamespace(id="bdv_1")),
            ),
            patch("cptr.routers.browser_device.browser_visual_frames.put", new=AsyncMock()) as put,
        ):
            await browser_device_visual_socket(socket)
        self.assertTrue(socket.accepted)
        self.assertEqual(socket.sent[0]["type"], "device.visual_authenticated")
        put.assert_awaited_once()
        frame = put.await_args.args[0]
        self.assertEqual(frame.frame_id, "frm_1")
        self.assertEqual(frame.data, b"hello")

    async def test_visual_websocket_rejects_malformed_frame_before_storage(self):
        socket = FakeWebSocket(
            [
                {
                    "protocol_version": 1,
                    "type": "device.authenticate",
                    "device_id": "bdv_1",
                    "device_credential": "secret",
                    "resume_from": 0,
                },
                {
                    "protocol_version": 1,
                    "type": "browser.frame",
                    "device_id": "bdv_1",
                    "session_id": "brs_1",
                    "frame_id": "frm_bad",
                    "mime_type": "image/jpeg",
                    "width": 640,
                    "height": 480,
                    "created_at_ms": 123,
                    "data_base64": "not base64!!",
                },
            ]
        )
        with (
            patch(
                "cptr.routers.browser_device.browser_device_store.authenticate_device",
                new=AsyncMock(return_value=SimpleNamespace(id="bdv_1")),
            ),
            patch("cptr.routers.browser_device.browser_visual_frames.put", new=AsyncMock()) as put,
        ):
            await browser_device_visual_socket(socket)
        self.assertEqual(socket.closed, [(1008, "invalid browser frame")])
        put.assert_not_awaited()

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
