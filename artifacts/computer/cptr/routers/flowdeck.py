"""Authenticated production entry gate for controlled Heidi orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cptr.flowdeck.authenticated_gateway import (
    AuthenticatedGatewayError,
    resolve_gateway_workspace,
)
from cptr.flowdeck.build import BuildContractError, build_initial_message, parse_build_request
from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.coordinator import (
    CoordinatorPolicyError,
    CoordinatorRequest,
    run_heidi_coordinator,
)
from cptr.flowdeck.durable import DurableFlowDeck, RunStatus
from cptr.models import Chat, ChatMessage
from cptr.utils.chat_export import export_chat_to_file
from cptr.utils.config import now_ms
from cptr.routers.gateway import _authenticate, _resolve_model
from cptr.utils.config import check_access
from cptr.utils.db import get_session_factory

router = APIRouter(prefix="/v1/flowdeck", tags=["flowdeck"])
logger = logging.getLogger(__name__)
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


class OrchestrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=4096)
    objective: str = Field(min_length=1, max_length=20_000)
    metadata: dict[str, Any] | None = None


class SteeringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1, max_length=20_000)


def _message_dict(message: ChatMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "parent_id": message.parent_id,
        "role": message.role,
        "content": message.content,
        "model": message.model,
        "done": message.done,
        "output": message.output,
        "usage": message.usage,
        "meta": message.meta,
        "created_at": message.created_at,
    }


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
    try:
        build_request = parse_build_request(body.objective)
    except BuildContractError as exc:
        raise HTTPException(422, str(exc)) from exc

    store = DurableFlowDeck(get_session_factory())
    try:
        workspace = await resolve_gateway_workspace(
            session_factory=store.session_factory,
            user_id=user_id,
            requested_workspace=body.workspace,
        )
        # Prefer the model already chosen in the authenticated chat. This
        # avoids an unnecessary provider-model discovery round trip for
        # FlowDeck requests while still resolving it through server policy.
        requested_model = str((body.metadata or {}).get("model") or "").strip()
        if requested_model:
            from cptr.utils.model_targets import resolve_model_target

            try:
                target = await resolve_model_target(requested_model, request.app.state)
            except Exception:
                target, _ = await _resolve_model(request, workspace, request.app.state)
        else:
            target, _ = await _resolve_model(request, workspace, request.app.state)
        if getattr(target, "kind", None) != "api":
            raise HTTPException(503, "the CPTR API model is unavailable")
        full_model_id = str(
            getattr(target, "full_model_id", None)
            or getattr(target, "runtime_model", None)
            or requested_model
        )
        metadata = body.metadata or {}
        chat_id = str(metadata.get("chat_id") or "").strip()
        parent_id = str(metadata.get("parent_id") or "").strip() or None
        chat = await Chat.get_by_id(chat_id) if chat_id else None
        if chat and chat.user_id != user_id:
            raise HTTPException(404, "chat not found")
        if chat is None:
            chat = await Chat.create(
                user_id=user_id,
                title=body.objective[:50].strip() or "New Chat",
                meta={"workspace": workspace, "model_id": full_model_id},
                created_at=now_ms(),
            )
        user_message = await ChatMessage.create(
            chat_id=chat.id,
            role="user",
            content=body.objective,
            parent_id=parent_id,
            model=full_model_id,
            meta={"agent": "heidi", "flowdeck": True},
            created_at=now_ms(),
        )
        assistant_message = await ChatMessage.create(
            chat_id=chat.id,
            role="assistant",
            content=build_initial_message(build_request) if build_request else "",
            parent_id=user_message.id,
            model=full_model_id,
            done=False,
            output=[],
            meta={
                "agent": "heidi",
                "flowdeck": True,
                **({"build_mode": True} if build_request else {}),
            },
            created_at=now_ms(),
        )
        await Chat.update_current_message(chat.id, assistant_message.id, now_ms())
        await export_chat_to_file(request, chat.id)

        # Reserve the durable run before returning. The coordinator continues
        # independently so the UI can render the persisted messages and begin
        # receiving native CPTR events immediately.
        run, _ = await store.create_run(
            request_key=request_key,
            owner=user_id,
            workspace=workspace,
            step_name="heidi-coordinator",
        )
        chat_meta = dict(chat.meta or {})
        chat_meta.update(
            {
                "flowdeck_run_id": run.id,
                "flowdeck_status": run.status.lower(),
            }
        )
        await Chat.update_meta(chat.id, chat_meta, now_ms())
        await ChatMessage.update(
            assistant_message.id,
            meta={
                "agent": "heidi",
                "flowdeck": True,
                "flowdeck_run_id": run.id,
            },
        )

        async def run_in_background() -> None:
            try:
                result = await run_heidi_coordinator(
                    CoordinatorRequest(
                        request_key=request_key,
                        task=body.objective,
                        workspace=workspace,
            model=getattr(target, "runtime_model", full_model_id),
                        connection=target.connection,
                        parent_chat_id=chat.id,
                        parent_message_id=assistant_message.id,
                        build_request=build_request,
                    ),
                    authenticated_request=request,
                    store=store,
                )
                content = result.message or (
                    "Heidi completed the FlowDeck orchestration."
                    if result.status == "succeeded"
                    else ""
                )
                await ChatMessage.update(
                    assistant_message.id,
                    content=content,
                    done=True,
                    meta={
                        "agent": "heidi",
                        "flowdeck": True,
                        "flowdeck_run_id": result.run_id,
                        "outcome": result.outcome,
                    },
                )
                await export_chat_to_file(request, chat.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("FlowDeck background orchestration failed")

        asyncio.create_task(run_in_background())
        # Let the task reach its first database checkpoint before returning.
        # This keeps the durable run and native child registration observable
        # to an immediate status, reconnect, or cancellation request.
        # A coordinator may perform several awaited durable setup operations
        # before its first observable state change. Give it a bounded chance
        # to publish that state so immediate retries and cancellation requests
        # cannot race an unstarted task.
        for _ in range(16):
            await asyncio.sleep(0)
    except (AuthenticatedGatewayError, CoordinatorPolicyError) as exc:
        raise HTTPException(403, str(exc)) from exc
    except Exception as exc:
        logger.exception("FlowDeck model or orchestration execution failed")
        raise HTTPException(503, "FlowDeck could not reach the configured API model") from exc
    return {
        "run_id": run.id,
        "status": run.status.lower(),
        "chat_id": chat.id,
        "user_message": _message_dict(user_message),
        "assistant_message": _message_dict(assistant_message),
    }


@router.post("/orchestrations/{run_id}/steer")
async def steer_orchestration(request: Request, run_id: str, body: SteeringRequest):
    """Persist one authenticated steering instruction for the active run.

    Steering is deliberately separate from cancellation and from the native
    CPTR queue. The coordinator consumes this durable record at its next safe
    checkpoint; this endpoint never injects data into a child executor.
    """
    user_id = await _authenticate_flowdeck(request)
    chat = await Chat.get_by_id(body.chat_id)
    if not chat or chat.user_id != user_id:
        raise HTTPException(404, "chat not found")
    workspace = str((chat.meta or {}).get("workspace") or "").strip()
    if not workspace:
        raise HTTPException(409, "chat has no FlowDeck workspace")
    store = DurableFlowDeck(get_session_factory())
    try:
        workspace = await resolve_gateway_workspace(
            session_factory=store.session_factory,
            user_id=user_id,
            requested_workspace=workspace,
        )
    except AuthenticatedGatewayError as exc:
        raise HTTPException(403, str(exc)) from exc
    run = await store.get_run_for_owner(
        run_id=run_id, owner=user_id, workspace=workspace
    )
    if not run:
        raise HTTPException(404, "orchestration run not found")
    if run.status not in {RunStatus.PENDING.value, RunStatus.RUNNING.value}:
        return {
            "ok": False,
            "accepted": False,
            "state": run.status.lower(),
            "message": "FlowDeck is no longer running; start a new Heidi prompt.",
        }

    chat_meta = dict(chat.meta or {})
    if chat_meta.get("flowdeck_run_id") != run.id:
        raise HTTPException(409, "chat is not attached to this FlowDeck run")

    from cptr.utils.chat_task import get_pending_input_lock

    request_key = _request_key(request)
    async with get_pending_input_lock(chat.id):
        messages = await ChatMessage.get_all_by_chat(chat.id)
        duplicate = next(
            (
                message
                for message in messages
                if (message.meta or {}).get("flowdeck_steering_key") == request_key
            ),
            None,
        )
        if duplicate:
            return {
                "ok": True,
                "accepted": True,
                "queued": True,
                "duplicate": True,
                "state": run.status.lower(),
                "chat_id": chat.id,
                "message_id": duplicate.id,
            }
        active_parent = next(
            (
                message
                for message in messages
                if message.role == "assistant"
                and not message.done
                and (message.meta or {}).get("flowdeck") is True
                and (
                    (message.meta or {}).get("flowdeck_run_id") == run.id
                    or (message.meta or {}).get("agent") == "heidi"
                )
            ),
            None,
        )
        if active_parent is None:
            return {
                "ok": False,
                "accepted": False,
                "state": run.status.lower(),
                "message": "FlowDeck has reached a safe terminal checkpoint; start a new Heidi prompt.",
            }
        # created_at is millisecond precision, so several fast submissions
        # can share a timestamp. Allocate a local monotonic position while
        # holding the per-chat input lock; the coordinator can then consume
        # instructions in the order users created them.
        latest_created_at = max(
            (int(message.created_at or 0) for message in messages), default=0
        )
        steering = await ChatMessage.create(
            chat_id=chat.id,
            role="user",
            content=body.instruction,
            parent_id=active_parent.id,
            model=(active_parent.model or ""),
            meta={
                "flowdeck": True,
                "flowdeck_steering": True,
                "flowdeck_run_id": run.id,
                "flowdeck_steering_key": request_key,
                "queued": True,
            },
            created_at=max(now_ms(), latest_created_at + 1),
        )
    return {
        "ok": True,
        "accepted": True,
        "queued": True,
        "duplicate": False,
        "state": run.status.lower(),
        "chat_id": chat.id,
        "message_id": steering.id,
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
    try:
        cancelled = (
            run
            if run.status == RunStatus.CANCELLED.value
            else await DurableFlowDeck(get_session_factory()).cancel_run(
                run_id=run.id, owner=user_id, workspace=canonical
            )
        )
    except Exception as exc:
        raise HTTPException(409, "orchestration cannot be cancelled in its current state") from exc
    # Durable cancellation is authoritative, but CPTR also needs to stop the
    # native child task that may currently be inside model/tool execution.
    from cptr.utils.chat_task import cancel_flowdeck_tasks

    await cancel_flowdeck_tasks(run.id)
    return _safe_run(cancelled)