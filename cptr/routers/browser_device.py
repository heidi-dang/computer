"""Secure CPTR browser-device pairing, owner controls, and device WebSocket transport."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from cptr.services.browser_command_results import browser_command_results
from cptr.services.browser_evaluate_approvals import browser_evaluate_approvals
from cptr.services.browser_device_connections import browser_device_connections
from cptr.services.browser_devices import browser_device_store
from cptr.services.browser_visual_frames import BrowserVisualFrame, browser_visual_frames
from cptr.services.control_auth import authenticate_control_request

router = APIRouter(prefix="/api/browser-device/v1", tags=["browser-device"])


class PairingRequestBody(BaseModel):
    device_name: str = Field(min_length=1, max_length=120)


class PairingClaimBody(BaseModel):
    pairing_id: str = Field(min_length=1, max_length=120)
    claim_secret: str = Field(min_length=32, max_length=1024)


class PairingApproveBody(BaseModel):
    pairing_id: str = Field(min_length=1, max_length=120)
    # Optional for authenticated MCP approval. Some hosts classify six-digit
    # pairing challenges as OTP-like secrets and block the tool call before it
    # reaches CPTR. The opaque pairing_id still selects the exact challenge,
    # while claim_secret remains extension-only and is required to claim.
    code: str | None = Field(default=None, pattern=r"^\d{6}$")


class OpenSessionBody(BaseModel):
    device_id: str = Field(min_length=1, max_length=120)
    tab_id: int | None = Field(default=None, ge=0, le=2_147_483_647)
    workbench_session_id: str | None = Field(default=None, max_length=120)
    surface_id: str | None = Field(default=None, max_length=200)


class TransferLeaseBody(BaseModel):
    expected_epoch: int = Field(ge=0)
    expected_owner: Literal["none", "agent", "human"]
    new_owner: Literal["none", "agent", "human"]
    fresh_snapshot_id: str | None = Field(default=None, max_length=200)


class SendCommandBody(BaseModel):
    command_id: str = Field(min_length=1, max_length=160)
    action: str = Field(min_length=1, max_length=120)
    expected_epoch: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    wait_seconds: float = Field(default=15.0, ge=0.1, le=60.0)


class EvaluateApprovalBody(BaseModel):
    expression: str = Field(min_length=1, max_length=20_000)


class ReturnToAgentBody(BaseModel):
    expected_epoch: int = Field(ge=0)
    wait_seconds: float = Field(default=15.0, ge=0.1, le=30.0)


class StreamConfigureBody(BaseModel):
    visible: bool
    max_fps: int = Field(ge=0, le=12)
    max_width: int = Field(ge=320, le=3_840)
    quality: int = Field(ge=20, le=90)


class HumanInputBody(BaseModel):
    command_id: str = Field(min_length=1, max_length=160)
    expected_epoch: int = Field(ge=0)
    input_type: Literal[
        "pointer_move", "pointer_down", "pointer_up", "click", "double_click",
        "wheel", "key_down", "key_up", "text_input", "touch_start",
        "touch_move", "touch_end", "focus", "blur", "viewport_resize",
        "drag_start", "drag_move", "drag_end",
    ]
    x: float | None = Field(default=None, ge=0.0, le=1.0)
    y: float | None = Field(default=None, ge=0.0, le=1.0)
    delta_x: float | None = None
    delta_y: float | None = None
    button: Literal["none", "left", "middle", "right", "back", "forward"] | None = None
    key: str | None = Field(default=None, max_length=128)
    code: str | None = Field(default=None, max_length=128)
    text: str | None = Field(default=None, max_length=20_000)
    modifiers: list[Literal["Alt", "Control", "Meta", "Shift"]] = Field(default_factory=list, max_length=4)
    pointer_id: int | None = Field(default=None, ge=0)
    width: float | None = Field(default=None, gt=0, le=16_384)
    height: float | None = Field(default=None, gt=0, le=16_384)
    sensitive: bool = False
    wait_seconds: float = Field(default=10.0, ge=0.1, le=30.0)


async def _control_user(request: Request, scope: str) -> str:
    try:
        return await authenticate_control_request(request, scope)
    except PermissionError as exc:
        message = str(exc)
        raise HTTPException(
            status_code=403 if message.startswith("missing required scope") else 401,
            detail="control-plane access denied",
        ) from exc


@router.post("/pairing/request")
async def request_pairing(body: PairingRequestBody):
    pairing = await browser_device_store.request_pairing(device_name=body.device_name)
    return {
        "pairing_id": pairing.pairing_id,
        "code": pairing.code,
        "claim_secret": pairing.claim_secret,
        "expires_at": pairing.expires_at,
    }


@router.post("/pairing/approve")
async def approve_pairing(request: Request, body: PairingApproveBody):
    user_id = await _control_user(request, "task:write")
    approved = await browser_device_store.approve_pairing(
        user_id=user_id,
        pairing_id=body.pairing_id,
        code=body.code,
    )
    if not approved:
        raise HTTPException(status_code=404, detail="pairing challenge is unavailable")
    return {"approved": True, "pairing_id": body.pairing_id}


@router.post("/pairing/claim")
async def claim_pairing(body: PairingClaimBody):
    claimed = await browser_device_store.claim_pairing(
        pairing_id=body.pairing_id,
        claim_secret=body.claim_secret,
    )
    if claimed is None:
        raise HTTPException(status_code=401, detail="pairing claim is invalid or expired")
    device, credential = claimed
    return {
        "status": "claimed",
        "device_id": device.id,
        "device_credential": credential,
    }


@router.get("/devices")
async def list_devices(request: Request):
    user_id = await _control_user(request, "task:read")
    devices = await browser_device_store.list_devices(user_id=user_id)
    for device in devices:
        device_id = device.get("device_id")
        device["connected"] = bool(
            isinstance(device_id, str)
            and await browser_device_connections.is_connected(device_id=device_id)
        )
    return {"devices": devices}


def _safe_discovered_tabs(result: dict[str, Any]) -> list[dict[str, Any]]:
    payload = result.get("payload")
    raw_tabs = payload.get("tabs") if isinstance(payload, dict) else None
    if not isinstance(raw_tabs, list):
        return []
    tabs: list[dict[str, Any]] = []
    for value in raw_tabs[:200]:
        if not isinstance(value, dict):
            continue
        tab_id = value.get("id")
        window_id = value.get("windowId")
        if not isinstance(tab_id, int) or tab_id < 0 or not isinstance(window_id, int) or window_id < 0:
            continue
        tabs.append({
            "id": tab_id,
            "windowId": window_id,
            "active": bool(value.get("active")),
            "title": str(value.get("title") or "")[:512],
            "url": str(value.get("url") or "")[:4096],
            "status": str(value.get("status") or "unknown")[:32],
            "pinned": bool(value.get("pinned")),
            "incognito": bool(value.get("incognito")),
        })
    return tabs


async def _discover_device_tabs(*, user_id: str, device_id: str, wait_seconds: float = 15.0) -> list[dict[str, Any]]:
    if not await browser_device_store.owns_active_device(user_id=user_id, device_id=device_id):
        raise HTTPException(status_code=404, detail="browser device not found")
    if not await browser_device_connections.is_connected(device_id=device_id):
        raise HTTPException(status_code=409, detail="browser device is offline")
    command_id = f"tabs_{uuid4().hex}"
    await browser_command_results.reserve(command_id)
    event = await browser_device_store.append_device_event(
        device_id=device_id,
        event_type="browser.tabs.dispatched",
        payload={"command_id": command_id},
    )
    delivered = await browser_device_connections.send_control(
        device_id=device_id,
        message={
            "protocol_version": 1,
            "type": "browser.command",
            "device_id": device_id,
            "session_id": f"device_{device_id}",
            "surface_id": f"device_{device_id}",
            "sequence": int(event.sequence),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "cptr",
            "mode": "OBSERVING",
            "command_id": command_id,
            "payload": {"action": "list_tabs", "args": {}},
        },
    )
    if not delivered:
        await browser_command_results.abandon(command_id)
        raise HTTPException(status_code=409, detail="browser device is offline")
    try:
        result = await browser_command_results.wait(command_id, timeout_seconds=wait_seconds)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="browser tab discovery timed out") from exc
    if result.get("type") != "browser.command.completed":
        raise HTTPException(status_code=409, detail="browser tab discovery failed")
    return _safe_discovered_tabs(result)


@router.get("/devices/{device_id}/tabs")
async def list_device_tabs(request: Request, device_id: str):
    user_id = await _control_user(request, "task:read")
    tabs = await _discover_device_tabs(user_id=user_id, device_id=device_id)
    return {"device_id": device_id, "tabs": tabs}


@router.post("/devices/{device_id}/revoke")
async def revoke_device(request: Request, device_id: str):
    user_id = await _control_user(request, "task:write")
    if not await browser_device_store.revoke_device(user_id=user_id, device_id=device_id):
        raise HTTPException(status_code=404, detail="browser device not found")
    return {"revoked": True, "device_id": device_id}


@router.post("/devices/{device_id}/rotate")
async def rotate_device_credential(request: Request, device_id: str):
    user_id = await _control_user(request, "task:write")
    credential = await browser_device_store.rotate_credential(user_id=user_id, device_id=device_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="active browser device not found")
    return {"device_id": device_id, "device_credential": credential}


@router.post("/sessions")
async def open_browser_session(request: Request, body: OpenSessionBody):
    user_id = await _control_user(request, "task:write")
    tab_id = body.tab_id
    if tab_id is None:
        tabs = await _discover_device_tabs(user_id=user_id, device_id=body.device_id)
        selected = next((tab for tab in tabs if tab.get("active") is True), tabs[0] if tabs else None)
        if selected is None:
            raise HTTPException(status_code=409, detail="browser device has no discoverable tabs")
        tab_id = int(selected["id"])
    try:
        session = await browser_device_store.open_session(
            user_id=user_id,
            device_id=body.device_id,
            tab_id=tab_id,
            workbench_session_id=body.workbench_session_id,
            surface_id=body.surface_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="browser device not found") from exc

    lease = await browser_device_store.session_lease(session_id=session.id)
    if lease is None:
        raise HTTPException(status_code=409, detail="browser lease is unavailable")
    try:
        acquired = await browser_device_store.transfer_lease(
            session_id=session.id,
            expected_epoch=int(lease["epoch"]),
            expected_owner="none",
            new_owner="agent",
        )
    except (KeyError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    command_id = f"attach_{session.id}_{acquired['epoch']}"
    await browser_command_results.reserve(command_id)
    event = await browser_device_store.append_device_event(
        device_id=session.device_id,
        event_type="browser.session.attach",
        payload={"session_id": session.id, "command_id": command_id, "tab_id": int(session.tab_id), "epoch": acquired["epoch"]},
    )
    delivered = await browser_device_connections.send_control(
        device_id=session.device_id,
        message={
            "protocol_version": 1,
            "type": "browser.command",
            "device_id": session.device_id,
            "session_id": session.id,
            "surface_id": session.surface_id or session.id,
            "sequence": int(event.sequence),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "cptr",
            "mode": "AGENT_CONTROL",
            "command_id": command_id,
            "payload": {
                "action": "attach",
                "expected_epoch": acquired["epoch"],
                "args": {"tab_id": int(session.tab_id)},
            },
        },
    )
    if not delivered:
        await browser_command_results.abandon(command_id)
        await browser_device_store.abort_session_bootstrap(
            session_id=session.id,
            expected_epoch=int(acquired["epoch"]),
        )
        raise HTTPException(status_code=409, detail="browser device is offline")
    try:
        attach_result = await browser_command_results.wait(command_id, timeout_seconds=15.0)
    except TimeoutError as exc:
        await browser_device_store.abort_session_bootstrap(
            session_id=session.id,
            expected_epoch=int(acquired["epoch"]),
        )
        raise HTTPException(status_code=504, detail="browser attach timed out") from exc
    if attach_result.get("type") != "browser.command.completed":
        await browser_device_store.abort_session_bootstrap(
            session_id=session.id,
            expected_epoch=int(acquired["epoch"]),
        )
        raise HTTPException(status_code=409, detail="browser attach failed")
    return {
        "session_id": session.id,
        "device_id": session.device_id,
        "tab_id": int(session.tab_id),
        "state": "AGENT_CONTROL",
        "surface_id": session.surface_id,
        "lease": acquired,
        "attach": attach_result.get("payload", {}),
    }


@router.get("/sessions/{session_id}/frame")
async def get_browser_frame(request: Request, session_id: str, after_frame_id: str | None = None):
    user_id = await _control_user(request, "task:read")
    session = await browser_device_store.get_session(user_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="browser session not found")
    frame = await browser_visual_frames.wait_next(
        device_id=session.device_id,
        after_frame_id=after_frame_id,
        timeout_seconds=15.0,
    )
    if frame is None or frame.session_id != session_id:
        return Response(status_code=204)
    return Response(
        content=frame.data,
        media_type=frame.mime_type,
        headers={
            "Cache-Control": "no-store",
            "X-CPTR-Frame-Id": frame.frame_id,
            "X-CPTR-Frame-Width": str(frame.width),
            "X-CPTR-Frame-Height": str(frame.height),
            "X-CPTR-Frame-Time": str(frame.created_at_ms),
        },
    )


@router.post("/sessions/{session_id}/stream-config")
async def configure_browser_stream(request: Request, session_id: str, body: StreamConfigureBody):
    user_id = await _control_user(request, "task:write")
    session = await browser_device_store.get_session(user_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="browser session not found")
    event = await browser_device_store.append_device_event(
        device_id=session.device_id,
        event_type="browser.stream.configure",
        payload={
            "session_id": session_id,
            "visible": body.visible,
            "max_fps": body.max_fps,
            "max_width": body.max_width,
            "quality": body.quality,
        },
    )
    delivered = await browser_device_connections.send_control(
        device_id=session.device_id,
        message={
            "protocol_version": 1,
            "type": "browser.stream.configure",
            "device_id": session.device_id,
            "session_id": session_id,
            "surface_id": session.surface_id or session_id,
            "sequence": int(event.sequence),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "cptr",
            "mode": session.state,
            "payload": body.model_dump(),
        },
    )
    if not delivered:
        raise HTTPException(status_code=409, detail="browser device is offline")
    return {"configured": True, "session_id": session_id, **body.model_dump()}


@router.post("/sessions/{session_id}/return-to-agent")
async def return_browser_to_agent(request: Request, session_id: str, body: ReturnToAgentBody):
    user_id = await _control_user(request, "task:write")
    session = await browser_device_store.get_session(user_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="browser session not found")
    try:
        await browser_device_store.assert_mutation(
            session_id=session_id,
            actor="human",
            expected_epoch=body.expected_epoch,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    command_id = f"handoff_prepare_{session_id}_{body.expected_epoch}"
    await browser_command_results.reserve(command_id)
    event = await browser_device_store.append_device_event(
        device_id=session.device_id,
        event_type="browser.handoff.prepare_return",
        payload={"session_id": session_id, "command_id": command_id, "epoch": body.expected_epoch},
    )
    delivered = await browser_device_connections.send_control(
        device_id=session.device_id,
        message={
            "protocol_version": 1,
            "type": "browser.handoff.prepare_return",
            "device_id": session.device_id,
            "session_id": session_id,
            "surface_id": session.surface_id or session_id,
            "sequence": int(event.sequence),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "cptr",
            "mode": "HUMAN_CONTROL",
            "command_id": command_id,
            "payload": {"expected_epoch": body.expected_epoch},
        },
    )
    if not delivered:
        await browser_command_results.abandon(command_id)
        raise HTTPException(status_code=409, detail="browser device is offline")
    try:
        prepared = await browser_command_results.wait(command_id, timeout_seconds=body.wait_seconds)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="browser handoff snapshot timed out") from exc
    if prepared.get("type") != "browser.command.completed":
        raise HTTPException(status_code=409, detail="browser handoff snapshot failed")
    payload = prepared.get("payload")
    snapshot_id = payload.get("snapshot_id") if isinstance(payload, dict) else None
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise HTTPException(status_code=409, detail="browser handoff did not return a fresh snapshot")

    try:
        result = await browser_device_store.transfer_lease(
            session_id=session_id,
            expected_epoch=body.expected_epoch,
            expected_owner="human",
            new_owner="agent",
            fresh_snapshot_id=snapshot_id,
        )
    except (KeyError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    returned_event = await browser_device_store.append_device_event(
        device_id=session.device_id,
        event_type="browser.handoff.returned",
        payload={
            "session_id": session_id,
            "tab_id": result["tab_id"],
            "owner": result["owner"],
            "epoch": result["epoch"],
            "snapshot_id": result["snapshot_id"],
            "state": result["state"],
        },
    )
    await browser_device_connections.send_control(
        device_id=session.device_id,
        message={
            "protocol_version": 1,
            "type": "browser.handoff.returned",
            "device_id": session.device_id,
            "session_id": session_id,
            "surface_id": session.surface_id or session_id,
            "sequence": int(returned_event.sequence),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "cptr",
            "mode": "AGENT_CONTROL",
            "payload": {
                "owner": result["owner"],
                "epoch": result["epoch"],
                "snapshot_id": result["snapshot_id"],
            },
        },
    )
    return result


@router.post("/sessions/{session_id}/human-input")
async def send_browser_human_input(request: Request, session_id: str, body: HumanInputBody):
    user_id = await _control_user(request, "task:write")
    session = await browser_device_store.get_session(user_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="browser session not found")
    try:
        await browser_device_store.assert_mutation(
            session_id=session_id,
            actor="human",
            expected_epoch=body.expected_epoch,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await browser_command_results.reserve(body.command_id)
    event = await browser_device_store.append_device_event(
        device_id=session.device_id,
        event_type="browser.human.input.dispatched",
        payload={
            "session_id": session_id,
            "command_id": body.command_id,
            "input_type": body.input_type,
            "sensitive": body.sensitive,
        },
    )
    payload = body.model_dump(exclude={"command_id", "wait_seconds"}, exclude_none=True)
    delivered = await browser_device_connections.send_control(
        device_id=session.device_id,
        message={
            "protocol_version": 1,
            "type": "browser.human.input",
            "device_id": session.device_id,
            "session_id": session_id,
            "surface_id": session.surface_id or session_id,
            "sequence": int(event.sequence),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "cptr",
            "mode": "HUMAN_CONTROL",
            "command_id": body.command_id,
            "payload": payload,
        },
    )
    if not delivered:
        await browser_command_results.abandon(body.command_id)
        raise HTTPException(status_code=409, detail="browser device is offline")
    try:
        result = await browser_command_results.wait(body.command_id, timeout_seconds=body.wait_seconds)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="human browser input timed out") from exc
    return {"accepted": True, "command_id": body.command_id, "result": result}


@router.post("/sessions/{session_id}/evaluate-approval")
async def approve_browser_evaluate(request: Request, session_id: str, body: EvaluateApprovalBody):
    user_id = await _control_user(request, "task:write")
    session = await browser_device_store.get_session(user_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="browser session not found")
    approval = browser_evaluate_approvals.issue(
        user_id=user_id,
        session_id=session_id,
        expression=body.expression,
    )
    return {
        "approval_token": approval.token,
        "expires_in_seconds": 120,
        "session_id": session_id,
    }


@router.post("/sessions/{session_id}/command")
async def send_browser_command(request: Request, session_id: str, body: SendCommandBody):
    user_id = await _control_user(request, "task:write")
    session = await browser_device_store.get_session(user_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="browser session not found")
    if body.expected_epoch is not None:
        try:
            await browser_device_store.assert_mutation(
                session_id=session_id,
                actor="agent",
                expected_epoch=body.expected_epoch,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    if body.action == "evaluate":
        expression = body.payload.get("expression")
        approval_token = body.payload.get("approval_token")
        if not isinstance(expression, str) or not isinstance(approval_token, str):
            raise HTTPException(status_code=403, detail="browser evaluate requires explicit approval")
        approved = browser_evaluate_approvals.consume(
            token=approval_token,
            user_id=user_id,
            session_id=session_id,
            expression=expression,
        )
        if not approved:
            raise HTTPException(status_code=403, detail="browser evaluate approval is invalid, expired, or already used")
    await browser_command_results.reserve(body.command_id)
    lease = await browser_device_store.session_lease(session_id=session_id)
    if lease is None:
        await browser_command_results.abandon(body.command_id)
        raise HTTPException(status_code=409, detail="browser lease is unavailable")
    command_event = await browser_device_store.append_device_event(
        device_id=session.device_id,
        event_type="browser.command.dispatched",
        payload={"session_id": session_id, "command_id": body.command_id, "action": body.action},
    )
    delivered = await browser_device_connections.send_control(
        device_id=session.device_id,
        message={
            "protocol_version": 1,
            "type": "browser.command",
            "device_id": session.device_id,
            "session_id": session_id,
            "surface_id": session.surface_id or session_id,
            "sequence": int(command_event.sequence),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "cptr",
            "mode": session.state,
            "command_id": body.command_id,
            "payload": {
                "action": body.action,
                "expected_epoch": body.expected_epoch,
                "args": body.payload,
            },
        },
    )
    if not delivered:
        await browser_command_results.abandon(body.command_id)
        raise HTTPException(status_code=409, detail="browser device is offline")
    try:
        result = await browser_command_results.wait(
            body.command_id,
            timeout_seconds=body.wait_seconds,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="browser command timed out") from exc
    return {
        "accepted": True,
        "command_id": body.command_id,
        "device_id": session.device_id,
        "result": result,
    }


@router.post("/sessions/{session_id}/lease")
async def transfer_browser_lease(request: Request, session_id: str, body: TransferLeaseBody):
    user_id = await _control_user(request, "task:write")
    session = await browser_device_store.get_session(user_id=user_id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="browser session not found")
    try:
        result = await browser_device_store.transfer_lease(
            session_id=session_id,
            expected_epoch=body.expected_epoch,
            expected_owner=body.expected_owner,
            new_owner=body.new_owner,
            fresh_snapshot_id=body.fresh_snapshot_id,
        )
        event_type = (
            "browser.handoff.returned"
            if body.expected_owner == "human" and body.new_owner == "agent"
            else "browser.lease.transferred"
        )
        handoff_event = await browser_device_store.append_device_event(
            device_id=session.device_id,
            event_type=event_type,
            payload={
                "session_id": session_id,
                "tab_id": result["tab_id"],
                "owner": result["owner"],
                "epoch": result["epoch"],
                "snapshot_id": result["snapshot_id"],
                "state": result["state"],
            },
        )
        control_type = (
            "browser.handoff.returned"
            if body.expected_owner == "human" and body.new_owner == "agent"
            else "browser.handoff.accepted"
            if body.new_owner == "human"
            else "browser.handoff.cancelled"
        )
        await browser_device_connections.send_control(
            device_id=session.device_id,
            message={
                "protocol_version": 1,
                "type": control_type,
                "device_id": session.device_id,
                "session_id": session_id,
                "surface_id": session.surface_id or session_id,
                "sequence": int(handoff_event.sequence),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "cptr",
                "mode": result["state"],
                "payload": {
                    "owner": result["owner"],
                    "epoch": result["epoch"],
                    "snapshot_id": result["snapshot_id"],
                },
            },
        )
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="browser session not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _receive_auth(websocket: WebSocket) -> tuple[str, str, int] | None:
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        message = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        return None
    if not isinstance(message, dict) or message.get("type") != "device.authenticate":
        return None
    if message.get("protocol_version") != 1:
        return None
    device_id = message.get("device_id")
    credential = message.get("device_credential")
    resume_from = message.get("resume_from", 0)
    if not isinstance(device_id, str) or not isinstance(credential, str):
        return None
    if not isinstance(resume_from, int) or resume_from < 0:
        return None
    return device_id, credential, resume_from


@router.websocket("/connect/visual")
async def browser_device_visual_socket(websocket: WebSocket):
    await websocket.accept()
    auth = await _receive_auth(websocket)
    if auth is None:
        await websocket.close(code=1008, reason="device authentication required")
        return
    device_id, credential, _resume_from = auth
    device = await browser_device_store.authenticate_device(device_id=device_id, credential=credential)
    if device is None:
        await websocket.close(code=1008, reason="device authentication failed")
        return
    await websocket.send_json({
        "protocol_version": 1,
        "type": "device.visual_authenticated",
        "device_id": device_id,
    })
    try:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                await websocket.close(code=1008, reason="invalid visual message")
                return
            if message.get("protocol_version") != 1 or message.get("device_id") != device_id:
                await websocket.close(code=1008, reason="visual protocol violation")
                return
            if message.get("type") != "browser.frame":
                await websocket.close(code=1008, reason="unsupported visual event")
                return
            session_id = message.get("session_id")
            frame_id = message.get("frame_id")
            mime_type = message.get("mime_type")
            width = message.get("width")
            height = message.get("height")
            created_at_ms = message.get("created_at_ms")
            data_b64 = message.get("data_base64")
            if not all([
                isinstance(session_id, str),
                isinstance(frame_id, str),
                isinstance(mime_type, str),
                isinstance(width, int),
                isinstance(height, int),
                isinstance(created_at_ms, int),
                isinstance(data_b64, str),
            ]):
                await websocket.close(code=1008, reason="invalid browser frame metadata")
                return
            import base64
            try:
                data = base64.b64decode(data_b64, validate=True)
                await browser_visual_frames.put(BrowserVisualFrame(
                    device_id=device_id,
                    session_id=session_id,
                    frame_id=frame_id,
                    mime_type=mime_type,
                    width=width,
                    height=height,
                    created_at_ms=created_at_ms,
                    data=data,
                ))
            except (ValueError, TypeError):
                await websocket.close(code=1008, reason="invalid browser frame")
                return
    except WebSocketDisconnect:
        return


@router.websocket("/connect/control")
async def browser_device_control_socket(websocket: WebSocket):
    await websocket.accept()
    auth = await _receive_auth(websocket)
    if auth is None:
        await websocket.close(code=1008, reason="device authentication required")
        return
    device_id, credential, resume_from = auth
    device = await browser_device_store.authenticate_device(
        device_id=device_id,
        credential=credential,
    )
    if device is None:
        await websocket.close(code=1008, reason="device authentication failed")
        return

    await browser_device_connections.attach(device_id=device_id, websocket=websocket)
    await websocket.send_json(
        {
            "protocol_version": 1,
            "type": "device.authenticated",
            "device_id": device_id,
            "resume_from": resume_from,
        }
    )
    # BrowserDeviceEvent is an internal audit log, not a wire-protocol replay
    # log. Replaying those rows produces envelopes missing session_id,
    # surface_id, timestamp/source/mode and can force the extension into a
    # reconnect loop. The extension restores its durable session/lease mirror;
    # new outbound commands may safely continue at any higher sequence.
    # In-flight commands interrupted by a disconnect time out and are retried
    # by their caller instead of replaying potentially sensitive payloads.
    _ = resume_from

    try:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                await websocket.close(code=1008, reason="invalid device message")
                return
            if message.get("protocol_version") != 1 or message.get("device_id") != device_id:
                await websocket.close(code=1008, reason="device protocol violation")
                return
            event_type = message.get("type")
            if not isinstance(event_type, str) or len(event_type) > 120:
                await websocket.close(code=1008, reason="invalid device event type")
                return
            payload = message.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            command_id = message.get("command_id")
            if event_type in {"browser.command.completed", "browser.command.failed"}:
                if not isinstance(command_id, str) or not command_id:
                    await websocket.close(code=1008, reason="browser result missing command id")
                    return
                await browser_command_results.complete(
                    command_id,
                    {
                        "type": event_type,
                        "command_id": command_id,
                        "payload": payload,
                    },
                )
            await browser_device_store.append_device_event(
                device_id=device_id,
                event_type=event_type,
                payload={"command_id": command_id, **payload} if isinstance(command_id, str) else payload,
            )
    except WebSocketDisconnect:
        return
    finally:
        await browser_device_connections.detach(device_id=device_id, websocket=websocket)
