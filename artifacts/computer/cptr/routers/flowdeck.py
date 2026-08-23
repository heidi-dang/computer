"""Authenticated production entry gate for controlled Heidi orchestration."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cptr.flowdeck.authenticated_gateway import (
    AuthenticatedGatewayError,
    resolve_gateway_workspace,
)
from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.coordinator import (
    CoordinatorPolicyError,
    CoordinatorRequest,
    run_heidi_coordinator,
)
from cptr.flowdeck.durable import DurableFlowDeck, RunStatus
from cptr.routers.gateway import _authenticate, _resolve_model
from cptr.utils.config import check_access
from cptr.utils.db import get_session_factory

router = APIRouter(prefix="/v1/flowdeck", tags=["flowdeck"])
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


class OrchestrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=4096)
    objective: str = Field(min_length=1, max_length=20_000)
    metadata: dict[str, Any] | None = None


def _request_key(request: Request) -> str:
    value = request.headers.get("Idempotency-Key", "").strip()
    if not _KEY_PATTERN.fullmatch(value):
        raise HTTPException(400, "a valid Idempotency-Key header is required")
    return value


async def _authenticate_flowdeck(request: Request) -> str:
    """Authenticate browser UI sessions without changing gateway auth.

    FlowDeck is exposed to the authenticated Computer web app, which uses the
    cptr_session cookie. Existing programmatic callers may still use the
    gateway's scoped Bearer token. The specialist gateway remains Bearer-only.
    """
    if request.headers.get("Authorization", "").startswith("Bearer "):
        return await _authenticate(request)

    session_token = request.cookies.get("cptr_session")
    if not session_token:
        # Preserve the original gateway-auth path for programmatic callers
        # and dependency-isolated tests that provide their own authenticator.
        return await _authenticate(request)

    auth = check_access(
        client_host=request.client.host if request.client else "127.0.0.1",
        jwt_token=session_token,
    )
    if auth is None or not auth.user_id:
        raise HTTPException(401, "Authentication required")
    request.state.auth = auth
    return auth.user_id


def _safe_run(run, *, reused: bool = False) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "request_key": hashlib.sha256(run.request_key.encode()).hexdigest()[:16],
        "workspace": run.workspace,
        "status": run.status.lower(),
        "reused": reused,
    }


async def _owned_run(request: Request, run_id: str, workspace: str):
    user_id = await _authenticate_flowdeck(request)
    try:
        canonical = await resolve_gateway_workspace(
            session_factory=get_session_factory(),
            user_id=user_id,
            requested_workspace=workspace,
        )
    except AuthenticatedGatewayError as exc:
        raise HTTPException(403, str(exc)) from exc
    run = await DurableFlowDeck(get_session_factory()).get_run_for_owner(
        run_id=run_id, owner=user_id, workspace=canonical
    )
    if not run:
        raise HTTPException(404, "orchestration run not found")
    return user_id, canonical, run


@router.post("/orchestrations", status_code=status.HTTP_200_OK)
async def create_orchestration(request: Request, body: OrchestrationRequest):
    """Run one controlled Heidi orchestration using server-owned policy."""
    user_id = await _authenticate_flowdeck(request)
    try:
        config = FlowDeckConfig.from_env()
    except (TypeError, ValueError) as exc:
        raise HTTPException(404, "controlled orchestration is unavailable") from exc
    if not (
        config.enabled
        and config.coordinator_enabled
        and config.mode.value == "controlled"
        and config.governance == "strict"
        and not config.global_kill_switch
    ):
        raise HTTPException(404, "controlled orchestration is unavailable")
    request_key = _request_key(request)

    store = DurableFlowDeck(get_session_factory())
    try:
        workspace = await resolve_gateway_workspace(
            session_factory=store.session_factory,
            user_id=user_id,
            requested_workspace=body.workspace,
        )
        target, _ = await _resolve_model(request, workspace, request.app.state)
        if target.kind != "api":
            raise HTTPException(503, "the CPTR API model is unavailable")
        result = await run_heidi_coordinator(
            CoordinatorRequest(
                request_key=request_key,
                task=body.objective,
                workspace=workspace,
                model=target.runtime_model,
                connection=target.connection,
                parent_chat_id="flowdeck-production",
            ),
            authenticated_request=request,
            store=store,
        )
    except (AuthenticatedGatewayError, CoordinatorPolicyError) as exc:
        raise HTTPException(403, str(exc)) from exc
    return {
        "run_id": result.run_id,
        "status": result.status,
        "children": list(result.children),
    }


@router.get("/orchestrations/{run_id}")
async def get_orchestration(request: Request, run_id: str, workspace: str):
    _, _, run = await _owned_run(request, run_id, workspace)
    events = await DurableFlowDeck(get_session_factory()).list_events(run.id)
    response = _safe_run(run)
    response["events"] = [
        {
            "id": event.id,
            "run_id": event.run_id,
            "sequence": event.sequence,
            "kind": event.kind,
            "payload": event.payload,
            "created_at": event.created_at,
        }
        for event in events
    ]
    return response


@router.post("/orchestrations/{run_id}/cancel")
async def cancel_orchestration(request: Request, run_id: str, workspace: str):
    user_id, canonical, run = await _owned_run(request, run_id, workspace)
    if run.status == RunStatus.CANCELLED.value:
        return _safe_run(run)
    try:
        cancelled = await DurableFlowDeck(get_session_factory()).cancel_run(
            run_id=run.id, owner=user_id, workspace=canonical
        )
    except Exception as exc:
        raise HTTPException(409, "orchestration cannot be cancelled in its current state") from exc
    return _safe_run(cancelled)