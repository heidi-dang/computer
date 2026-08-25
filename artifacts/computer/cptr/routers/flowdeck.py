"""Authenticated production entry gate for controlled Heidi orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
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
from cptr.flowdeck.designer import DesignerContractError, DesignerRequest, run_designer
from cptr.flowdeck.runtime import RuntimeContractError, RuntimeRequest, managed_runtime
from cptr.flowdeck.database import (
    DatabaseCancelledError,
    DatabaseContractError,
    DatabaseRequest,
    cancel_database_operation,
    project_database,
    register_database_operation,
    unregister_database_operation,
)
from cptr.flowdeck.generated_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    GeneratedAuthError,
    GeneratedAuthService,
)
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


class AuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=4096)
    objective: str = Field(min_length=1, max_length=20_000)
    scope: dict[str, Any] = Field(default_factory=dict)
    completion_contract: list[str] = Field(min_length=1, max_length=100)
    metadata: dict[str, Any] | None = None


class SteeringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1, max_length=20_000)


class DesignerRequestBody(BaseModel):
    """Deterministic Designer request; authority fields are server-owned."""
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=4096)
    operation: str = Field(min_length=1, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)


class RuntimeRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=4096)
    requested_port: int | None = Field(default=None, ge=3000, le=9000)


class DatabaseRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=4096)
    engine: str = Field(default="sqlite", pattern="^(sqlite|postgresql)$")
    database: str | None = Field(default=None, max_length=512)


class DatabaseQueryBody(DatabaseRequestBody):
    sql: str = Field(min_length=1, max_length=20_000)
    params: list[Any] = Field(default_factory=list, max_length=100)


class DatabaseMigrationBody(DatabaseRequestBody):
    sql: str = Field(min_length=1, max_length=20_000)
    snapshot: bool = True


class DatabaseRestoreBody(DatabaseRequestBody):
    snapshot: str = Field(min_length=1, max_length=512)


class GeneratedAuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=4096)


class GeneratedAuthCredentials(GeneratedAuthRequest):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=512)
    csrf: str = Field(min_length=1, max_length=256)


class GeneratedAuthCallback(GeneratedAuthRequest):
    issuer: str = Field(min_length=1, max_length=2048)
    audience: str = Field(min_length=1, max_length=512)
    redirect_uri: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=512)
    nonce: str = Field(min_length=1, max_length=512)
    code_verifier: str = Field(min_length=43, max_length=128)


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


def _audit_fingerprint(
    *,
    objective: str,
    scope: dict[str, Any],
    completion_contract: list[str],
    model: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "objective": objective,
                "scope": scope,
                "completion_contract": completion_contract,
                "model": model,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


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


def _same_origin(request: Request) -> None:
    origin = request.headers.get("Origin")
    if not origin:
        return
    expected = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    if origin.rstrip("/") != expected.rstrip("/"):
        raise HTTPException(403, "cross-origin request denied")


async def _generated_auth_service(request: Request, workspace: str) -> tuple[str, GeneratedAuthService]:
    user_id = await _authenticate_flowdeck(request)
    try:
        canonical = await resolve_gateway_workspace(
            session_factory=get_session_factory(),
            user_id=user_id,
            requested_workspace=workspace,
        )
        return user_id, GeneratedAuthService(canonical)
    except (AuthenticatedGatewayError, GeneratedAuthError) as exc:
        raise HTTPException(getattr(exc, "status", 403), str(exc)) from exc


def _auth_user_payload(user) -> dict[str, Any]:
    return {"id": user.id, "email": user.email, "role": user.role, "provider": user.provider}


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


async def _create_orchestration(
    request: Request,
    body: OrchestrationRequest,
    *,
    audit_contract: dict[str, Any] | None = None,
):
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
        audit_run = None
        if audit_contract is not None:
            # Reserve the audit run before creating transcript messages. This
            # makes the idempotency key cover the complete audit request, not
            # only the coordinator's later child dispatch.
            audit_run, audit_created = await store.create_run(
                request_key=request_key,
                owner=user_id,
                workspace=workspace,
                step_name="heidi-audit",
            )
            if not audit_created:
                events = await store.list_events(audit_run.id)
                scope_event = next(
                    (event for event in events if event.kind == "AUDIT_SCOPE_CREATED"),
                    None,
                )
                expected_fingerprint = _audit_fingerprint(
                    objective=body.objective,
                    scope=audit_contract["scope"],
                    completion_contract=audit_contract["completion_contract"],
                    model=full_model_id,
                )
                if not scope_event or scope_event.payload.get("fingerprint") != expected_fingerprint:
                    raise HTTPException(
                        409,
                        "Idempotency-Key is already used for a different audit request",
                    )
                return {
                    **_safe_run(audit_run, reused=True),
                    "audit": True,
                    "reused": True,
                }
        chat_id = str(metadata.get("chat_id") or "").strip()
        parent_id = str(metadata.get("parent_id") or "").strip() or None
        chat = await Chat.get_by_id(chat_id) if chat_id else None
        if chat and chat.user_id != user_id:
            raise HTTPException(404, "chat not found")
        if chat is None:
            chat = await Chat.create(
                user_id=user_id,
                title=body.objective[:50].strip() or "New Chat",
                meta={
                    "workspace": workspace,
                    "model_id": full_model_id,
                    **(
                        {
                            "audit": True,
                            "audit_scope": audit_contract["scope"],
                            "audit_completion_contract": audit_contract["completion_contract"],
                        }
                        if audit_contract
                        else {}
                    ),
                },
                created_at=now_ms(),
            )
        user_message = await ChatMessage.create(
            chat_id=chat.id,
            role="user",
            content=body.objective,
            parent_id=parent_id,
            model=full_model_id,
            meta={
                "agent": "heidi",
                "flowdeck": True,
                **({"audit": True} if audit_contract else {}),
            },
            created_at=now_ms(),
        )
        assistant_message = await ChatMessage.create(
            chat_id=chat.id,
            role="assistant",
            content=(
                build_initial_message(build_request)
                if build_request
                else (
                    "Heidi audit started. I will inspect the selected repository "
                    "using qualified read-only capabilities and report only "
                    "evidence-backed results."
                    if audit_contract
                    else ""
                )
            ),
            parent_id=user_message.id,
            model=full_model_id,
            done=False,
            output=[],
            meta={
                "agent": "heidi",
                "flowdeck": True,
                **({"build_mode": True} if build_request else {}),
                **(
                    {
                        "audit": True,
                        "audit_scope": audit_contract["scope"],
                        "audit_completion_contract": audit_contract["completion_contract"],
                    }
                    if audit_contract
                    else {}
                ),
            },
            created_at=now_ms(),
        )
        await Chat.update_current_message(chat.id, assistant_message.id, now_ms())
        await export_chat_to_file(request, chat.id)

        # Reserve the durable run before returning. The coordinator continues
        # independently so the UI can render the persisted messages and begin
        # receiving native CPTR events immediately.
        run = audit_run
        if run is None:
            run, _ = await store.create_run(
                request_key=request_key,
                owner=user_id,
                workspace=workspace,
                step_name="heidi-coordinator",
            )
        if audit_contract:
            await store.record_event(
                run.id,
                "AUDIT_SCOPE_CREATED",
                {
                    "scope": audit_contract["scope"],
                    "completion_contract": audit_contract["completion_contract"],
                    "model": full_model_id,
                    "fingerprint": _audit_fingerprint(
                        objective=body.objective,
                        scope=audit_contract["scope"],
                        completion_contract=audit_contract["completion_contract"],
                        model=full_model_id,
                    ),
                },
            )
            await store.record_event(
                run.id,
                "AUDIT_COMPLETION_CONTRACT_CREATED",
                {
                    "checks": audit_contract["completion_contract"],
                    "unknown_is_not_pass": True,
                },
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
                **({"audit": True} if audit_contract else {}),
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
                        audit_contract=audit_contract,
                    ),
                    authenticated_request=request,
                    store=store,
                )
                content = result.message or (
                    "Heidi completed the FlowDeck orchestration."
                    if result.status == "succeeded"
                    else "FlowDeck requires manual review before completion."
                )
                terminal_result = result.status in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "manual_review",
                    "manual_review_required",
                }
                if terminal_result:
                    chat_meta = dict(chat.meta or {})
                    chat_meta["flowdeck_run_id"] = result.run_id
                    chat_meta["flowdeck_status"] = result.status
                    await Chat.update_meta(chat.id, chat_meta, now_ms())
                message_meta = dict(assistant_message.meta or {})
                message_meta.update(
                    {
                        "agent": "heidi",
                        "flowdeck": True,
                        "flowdeck_run_id": result.run_id,
                        "outcome": result.outcome,
                        **({"flowdeck_status": result.status} if terminal_result else {}),
                    }
                )
                await ChatMessage.update(
                    assistant_message.id,
                    **({"content": content, "done": True} if terminal_result else {}),
                    meta=message_meta,
                )
                await export_chat_to_file(request, chat.id)
            except asyncio.CancelledError:
                logger.warning(
                    "FlowDeck background orchestration task cancelled: %s",
                    request_key,
                )
                raise
            except Exception:
                logger.exception("FlowDeck background orchestration failed")

        # Keep a strong reference while the coordinator is running. The event
        # loop only weakly tracks Tasks; without ownership, a background
        # coordinator can be garbage-collected while a child tester/model call
        # is awaiting, leaving durable attempts prepared and the parent in an
        # UNKNOWN/manual-review state.
        flowdeck_tasks = getattr(request.app.state, "flowdeck_tasks", None)
        if flowdeck_tasks is None:
            flowdeck_tasks = set()
            request.app.state.flowdeck_tasks = flowdeck_tasks
        background_task = asyncio.create_task(run_in_background())
        flowdeck_tasks.add(background_task)
        background_task.add_done_callback(flowdeck_tasks.discard)
        # Let the task reach its first database checkpoint before returning.
        # This keeps the durable run and native child registration observable
        # to an immediate status, reconnect, or cancellation request.
        # A coordinator may perform several awaited durable setup operations
        # before its first observable state change. Give it a bounded chance
        # to publish that state so immediate retries and cancellation requests
        # cannot race an unstarted task.
        for _ in range(16):
            await asyncio.sleep(0)
    except HTTPException:
        raise
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
        **({"audit": True, "reused": False} if audit_contract else {}),
    }


@router.post("/orchestrations", status_code=status.HTTP_200_OK)
async def create_orchestration(request: Request, body: OrchestrationRequest):
    """Run one controlled Heidi orchestration using server-owned policy."""
    return await _create_orchestration(request, body)


@router.post("/design", status_code=status.HTTP_200_OK)
async def create_design(request: Request, body: DesignerRequestBody):
    """Run one authenticated, bounded, read-only Designer operation.

    This route produces durable DESIGN_RESULT_CREATED evidence and never
    invokes a model or writes workspace files.
    """
    user_id = await _authenticate_flowdeck(request)
    try:
        config = FlowDeckConfig.from_env()
        if not (
            config.enabled
            and config.mode.value in {"read_only", "controlled"}
            and config.governance == "strict"
            and not config.global_kill_switch
        ):
            raise HTTPException(404, "designer is unavailable")
        workspace = await resolve_gateway_workspace(
            session_factory=get_session_factory(),
            user_id=user_id,
            requested_workspace=body.workspace,
        )
        result = await run_designer(
            DesignerRequest(
                request_key=_request_key(request),
                operation=body.operation,
                workspace=workspace,
                user_id=user_id,
                input=body.input,
            ),
            store=DurableFlowDeck(get_session_factory()),
        )
        return result
    except HTTPException:
        raise
    except AuthenticatedGatewayError as exc:
        raise HTTPException(403, str(exc)) from exc
    except DesignerContractError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/design/{run_id}")
async def get_design(request: Request, run_id: str, workspace: str):
    """Reconnect to Designer evidence using the shared authenticated status path."""
    return await get_orchestration(request, run_id, workspace)


@router.post("/audits", status_code=status.HTTP_200_OK)
async def create_audit(request: Request, body: AuditRequest):
    """Start one durable, evidence-bound Heidi repository audit."""
    if not body.scope:
        raise HTTPException(422, "audit scope must not be empty")
    contract = {
        "scope": body.scope,
        "completion_contract": list(body.completion_contract),
    }
    metadata = dict(body.metadata or {})
    metadata["audit_contract"] = contract
    return await _create_orchestration(
        request,
        OrchestrationRequest(
            workspace=body.workspace,
            objective=body.objective,
            metadata=metadata,
        ),
        audit_contract=contract,
    )


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


@router.get("/generated-auth/config")
async def generated_auth_config(request: Request, workspace: str):
    _same_origin(request)
    _, service = await _generated_auth_service(request, workspace)
    return service.metadata()


@router.get("/generated-auth/csrf")
async def generated_auth_csrf(request: Request, workspace: str, response: Response):
    _same_origin(request)
    _, service = await _generated_auth_service(request, workspace)
    token = service.issue_csrf()
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=60 * 60,
        httponly=False,
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    return {"csrf": token}


@router.post("/generated-auth/signup")
async def generated_auth_signup(request: Request, body: GeneratedAuthCredentials):
    _same_origin(request)
    _, service = await _generated_auth_service(request, body.workspace)
    if not hmac.compare_digest(body.csrf, request.cookies.get(CSRF_COOKIE, "")):
        raise HTTPException(403, "CSRF validation failed")
    try:
        user = service.signup(body.email, body.password)
    except GeneratedAuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"user": _auth_user_payload(user), "provider": service.provider.value}


@router.post("/generated-auth/signin")
async def generated_auth_signin(request: Request, body: GeneratedAuthCredentials, response: Response):
    _same_origin(request)
    _, service = await _generated_auth_service(request, body.workspace)
    if not hmac.compare_digest(body.csrf, request.cookies.get(CSRF_COOKIE, "")):
        raise HTTPException(403, "CSRF validation failed")
    try:
        user, session_token, csrf_token, expires_at = service.signin(body.email, body.password)
    except GeneratedAuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=60 * 60 * 24 * 7,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=60 * 60,
        httponly=False,
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    return {"user": _auth_user_payload(user), "expires_at": expires_at, "provider": service.provider.value}


@router.get("/generated-auth/session")
async def generated_auth_session(request: Request, workspace: str):
    _same_origin(request)
    _, service = await _generated_auth_service(request, workspace)
    try:
        session = service.session(request.cookies.get(SESSION_COOKIE, ""))
    except GeneratedAuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"user": _auth_user_payload(session.user), "expires_at": session.expires_at}


@router.get("/generated-auth/protected")
async def generated_auth_protected(request: Request, workspace: str, role: str | None = None):
    _same_origin(request)
    _, service = await _generated_auth_service(request, workspace)
    try:
        session = service.session(request.cookies.get(SESSION_COOKIE, ""))
        if role:
            service.require_role(session, role)
    except GeneratedAuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"ok": True, "user": _auth_user_payload(session.user)}


@router.post("/generated-auth/signout")
async def generated_auth_signout(request: Request, body: GeneratedAuthRequest, response: Response):
    _same_origin(request)
    _, service = await _generated_auth_service(request, body.workspace)
    try:
        service.signout(
            request.cookies.get(SESSION_COOKIE, ""),
            request.cookies.get(CSRF_COOKIE, ""),
        )
    except GeneratedAuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return {"ok": True}


@router.post("/generated-auth/callback/verify")
async def generated_auth_callback(request: Request, body: GeneratedAuthCallback):
    _same_origin(request)
    _, service = await _generated_auth_service(request, body.workspace)
    try:
        service.verify_external_callback(
            issuer=body.issuer,
            audience=body.audience,
            redirect_uri=body.redirect_uri,
            state=body.state,
            expected_state=request.cookies.get(CSRF_COOKIE, ""),
            nonce=body.nonce,
            expected_nonce=request.cookies.get(CSRF_COOKIE, ""),
            code_verifier=body.code_verifier,
        )
    except GeneratedAuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"verified": True, "provider": service.provider.value}


@router.post("/runtime/start")
async def start_runtime(request: Request, body: RuntimeRequestBody):
    user_id = await _authenticate_flowdeck(request)
    store = DurableFlowDeck(get_session_factory())
    try:
        workspace = await resolve_gateway_workspace(
            session_factory=store.session_factory,
            user_id=user_id,
            requested_workspace=body.workspace,
        )
        result = await managed_runtime.start(
            RuntimeRequest(
                request_key=_request_key(request),
                workspace=workspace,
                owner=user_id,
                requested_port=body.requested_port,
            ),
            store=store,
        )
    except (AuthenticatedGatewayError, RuntimeContractError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return result


@router.get("/runtime/{run_id}")
async def runtime_status(request: Request, run_id: str, workspace: str):
    _, _, run = await _owned_run(request, run_id, workspace)
    try:
        return await managed_runtime.status(run.id, store=DurableFlowDeck(get_session_factory()))
    except RuntimeContractError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/runtime/{run_id}/stop")
async def stop_runtime(request: Request, run_id: str, workspace: str):
    _, _, run = await _owned_run(request, run_id, workspace)
    try:
        return await managed_runtime.stop(run.id, store=DurableFlowDeck(get_session_factory()))
    except RuntimeContractError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/runtime/{run_id}/preview")
async def runtime_preview(request: Request, run_id: str, workspace: str):
    import httpx

    _, _, run = await _owned_run(request, run_id, workspace)
    status_data = await managed_runtime.status(run.id, store=DurableFlowDeck(get_session_factory()))
    if status_data.get("state") != "running":
        raise HTTPException(409, "managed runtime is not healthy")
    try:
        async with httpx.AsyncClient() as client:
            upstream = await client.get(f"http://127.0.0.1:{status_data['port']}/", timeout=10)
    except httpx.HTTPError as exc:
        raise HTTPException(503, "managed preview is temporarily unavailable") from exc
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/html"),
    )


async def _database_request(request: Request, body: DatabaseRequestBody) -> tuple[str, str, DurableFlowDeck, DatabaseRequest]:
    user_id = await _authenticate_flowdeck(request)
    store = DurableFlowDeck(get_session_factory())
    try:
        workspace = await resolve_gateway_workspace(
            session_factory=store.session_factory,
            user_id=user_id,
            requested_workspace=body.workspace,
        )
    except AuthenticatedGatewayError as exc:
        raise HTTPException(403, str(exc)) from exc
    return user_id, workspace, store, DatabaseRequest(
        request_key=_request_key(request),
        owner=user_id,
        workspace=workspace,
        engine=body.engine,
        database=body.database,
    )


async def _database_run(store: DurableFlowDeck, database_request: DatabaseRequest, operation: str, callback):
    run, created = await store.create_run(
        request_key=database_request.request_key,
        owner=database_request.owner,
        workspace=database_request.workspace,
        step_name=f"database-{operation}",
    )
    if not created:
        events = await store.list_events(run.id)
        result = next((event.payload.get("result") for event in events if event.kind == "DATABASE_RESULT"), None)
        if result is not None:
            return result
        if run.status == RunStatus.CANCELLED.value:
            raise DatabaseCancelledError("database operation was cancelled")
        if run.status == RunStatus.RUNNING.value:
            await store.require_manual_review(
                run.id,
                reason="database operation was interrupted before its outcome was durable",
            )
            raise DatabaseCancelledError("database operation requires reconciliation after interruption")
    await store.start_run(run.id)
    await store.record_event(run.id, "DATABASE_OPERATION_STARTED", {"operation": operation, "authoritative": True, "source": "runtime"})
    handle = register_database_operation(run.id)
    try:
        result = await callback(handle)
        current = await store.get_run(run.id)
        if current is None or current.status == RunStatus.CANCELLED.value or handle.cancel_event.is_set():
            await store.record_event(
                run.id,
                "DATABASE_LATE_OUTCOME_DISCARDED",
                {"operation": operation, "authoritative": True, "source": "verifier"},
            )
            raise DatabaseCancelledError("database outcome was discarded after cancellation")
        result["run_id"] = run.id
        result["operation"] = operation
        result["evidence"] = {
            "authoritative": True,
            "source": "verifier",
            "observation": "verifier_check",
            "observed_outcome": "succeeded",
            "database_engine": database_request.engine,
        }
        if operation == "migrate" and "migration_history" in result:
            await store.record_event(
                run.id,
                "DATABASE_MIGRATION_CHECKPOINT",
                {"migration_history": result["migration_history"], "authoritative": True, "source": "verifier"},
            )
        await store.record_event(run.id, "DATABASE_RESULT", {"result": result})
        return result
    except DatabaseCancelledError:
        if handle.cancel_event.is_set():
            try:
                current = await store.get_run(run.id)
                if current is not None and current.status != RunStatus.CANCELLED.value:
                    await store.cancel_run(
                        run_id=run.id,
                        owner=database_request.owner,
                        workspace=database_request.workspace,
                    )
            except Exception:
                pass
        current = await store.get_run(run.id)
        if current is not None and current.status != RunStatus.CANCELLED.value:
            try:
                await store.record_event(
                    run_id=run.id,
                    kind="DATABASE_OPERATION_CANCELLED",
                    payload={"operation": operation, "authoritative": True, "source": "runtime"},
                )
            except Exception:
                pass
        raise
    except DatabaseContractError:
        await store.record_event(run.id, "DATABASE_OPERATION_DENIED", {"operation": operation, "authoritative": True, "source": "verifier"})
        raise
    finally:
        unregister_database_operation(run.id)


@router.post("/database/inspect")
async def inspect_database(request: Request, body: DatabaseRequestBody):
    _, _, store, database_request = await _database_request(request, body)
    try:
        return await _database_run(store, database_request, "inspect", lambda handle: project_database.inspect(database_request, handle=handle))
    except DatabaseCancelledError as exc:
        raise HTTPException(409, str(exc)) from exc
    except DatabaseContractError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/database/query")
async def query_database(request: Request, body: DatabaseQueryBody):
    _, _, store, database_request = await _database_request(request, body)
    try:
        return await _database_run(store, database_request, "query", lambda handle: project_database.query(database_request, body.sql, body.params, handle=handle))
    except DatabaseCancelledError as exc:
        raise HTTPException(409, str(exc)) from exc
    except DatabaseContractError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/database/migrate")
async def migrate_database(request: Request, body: DatabaseMigrationBody):
    _, _, store, database_request = await _database_request(request, body)
    try:
        return await _database_run(store, database_request, "migrate", lambda handle: project_database.migrate(database_request, body.sql, snapshot=body.snapshot, handle=handle))
    except DatabaseCancelledError as exc:
        raise HTTPException(409, str(exc)) from exc
    except DatabaseContractError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/database/restore")
async def restore_database(request: Request, body: DatabaseRestoreBody):
    _, _, store, database_request = await _database_request(request, body)
    try:
        return await _database_run(
            store,
            database_request,
            "restore",
            lambda _handle: project_database.restore(database_request, body.snapshot),
        )
    except DatabaseCancelledError as exc:
        raise HTTPException(409, str(exc)) from exc
    except DatabaseContractError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/audits/{run_id}")
async def get_audit(request: Request, run_id: str, workspace: str):
    """Reconnect to a durable audit through the shared FlowDeck status path."""
    return await get_orchestration(request, run_id, workspace)


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
    cancel_database_operation(run.id)
    return _safe_run(cancelled)


@router.post("/audits/{run_id}/cancel")
async def cancel_audit(request: Request, run_id: str, workspace: str):
    """Cancel an audit using the authoritative FlowDeck cancellation path."""
    return await cancel_orchestration(request, run_id, workspace)


@router.post("/audits/{run_id}/steer")
async def steer_audit(request: Request, run_id: str, body: SteeringRequest):
    """Steer an active audit without creating a second run."""
    return await steer_orchestration(request, run_id, body)