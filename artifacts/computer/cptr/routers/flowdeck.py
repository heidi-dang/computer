"""Authenticated production entry gate for controlled Heidi orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

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
from cptr.flowdeck.durable import (
    build_audit_summary,
    DurableFlowDeck,
    DuplicateRequestError,
    LifecycleError,
    OperationStatus,
    RunStatus,
    StepStatus,
    StaleWriterError,
)
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
    CSRF_TTL_SECONDS,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    GeneratedAuthError,
    GeneratedAuthService,
)
from cptr.flowdeck.fdx import (
    FDX_CONTAINMENT_CATEGORIES,
    FDX_CONTAINMENT_EVENT_KIND,
    safe_containment_category,
)
from cptr.flowdeck.checkpoints import CheckpointError, CheckpointService
from cptr.flowdeck.adaptive import adaptive_route
from cptr.models import Chat, ChatMessage
from cptr.models.flowdeck import FlowDeckEvent, FlowDeckRun
from cptr.utils.chat_export import export_chat_to_file
from cptr.utils.config import now_ms
from cptr.routers.gateway import _authenticate, _resolve_model
from cptr.utils.config import check_access
from cptr.utils.db import get_session_factory

router = APIRouter(prefix="/v1/flowdeck", tags=["flowdeck"])
logger = logging.getLogger(__name__)
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")
_SAFE_RUN_STATUSES = frozenset(
    {
        "pending",
        "running",
        "orphaned",
        "recovering",
        "succeeded",
        "failed",
        "manual_review_required",
        "cancelled",
    }
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FDX_DIAGNOSTIC_SCAN_MULTIPLIER = 10


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


class NewRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=4096)
    objective: str = Field(min_length=1, max_length=20_000)
    original_run_id: str = Field(min_length=1, max_length=128)
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

async def _parse_generated_auth_callback(request: Request) -> GeneratedAuthCallback:
    """Validate callback input without exposing rejected credential-shaped values."""
    try:
        payload = await request.json()
        return GeneratedAuthCallback.model_validate(payload)
    except (ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(422, "invalid callback payload") from exc


class CheckpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=4096)


class CheckpointRestoreRequest(CheckpointRequest):
    checkpoint_id: str = Field(min_length=1, max_length=128)


async def _generated_auth_operation(
    request: Request,
    *,
    workspace: str,
    capability: str,
    target: str,
    execute,
) -> tuple[dict[str, Any], bool, str]:
    """Run one generated-auth action through the durable FlowDeck state machine.

    The adapter is deliberately called only after intent and an attempt are
    durable. Results are verifier evidence and never contain bearer secrets.
    """
    owner, service = await _generated_auth_service(request, workspace)
    request_key = _request_key(request)
    store = DurableFlowDeck(get_session_factory())
    try:
        run, created = await store.create_run(
            request_key=request_key, owner=owner, workspace=str(service.workspace),
            step_name=f"generated-auth:{capability}",
        )
        if not created:
            operations = await store.get_run_operations(run.id)
            if operations:
                operation = operations[0]
                evidence = operation.authoritative_evidence or {}
                expected_capability = f"generated_auth.{capability}"
                if (
                    operation.capability != expected_capability
                    or operation.target != target
                ):
                    raise HTTPException(
                        409,
                        "Idempotency-Key is already used for a different authentication operation",
                    )
                if operation.status == OperationStatus.SUCCEEDED.value:
                    return dict(evidence.get("result") or {}), True, run.id
                if operation.status == OperationStatus.FAILED.value:
                    raise HTTPException(
                        int(evidence.get("error_status", 422)),
                        evidence.get("error", "authentication operation failed"),
                    )
                if run.status == RunStatus.CANCELLED.value:
                    raise HTTPException(409, "authentication operation was cancelled")
                raise HTTPException(409, "authentication operation requires recovery")

        await store.start_run(run.id)
        step = await store.get_step(run.id)
        await store.start_step(step.id)
        operation, _ = await store.record_intent(
            run_id=run.id, step_id=step.id, idempotency_key=request_key,
            capability=f"generated_auth.{capability}", target=target,
            reconcile_kind="generated_auth",
        )
        attempt = await store.prepare_attempt(operation_id=operation.id, owner=owner)
        await store.record_event(
            run.id, "AUTH_OPERATION_STARTED",
            {"operation": capability, "attempt_id": attempt.id, "authoritative": True,
             "source": "runtime"},
        )
        try:
            result = execute(service)
            if inspect.isawaitable(result):
                result = await result
            public_result = {
                key: value for key, value in result.items()
                if not key.startswith("_")
            } if isinstance(result, dict) else result
            evidence = {
                "authoritative": True, "source": "verifier",
                "observation": "verifier_check", "observed_outcome": "succeeded",
                "attempt_id": attempt.id, "operation": capability,
                "result": public_result,
            }
            await store.finish_attempt(
                attempt.id, owner=owner, outcome="succeeded", evidence=evidence,
            )
            await store.finish_step(step.id, status=StepStatus.SUCCEEDED)
            await store.record_event(
                run.id, "AUTH_OPERATION_VERIFIED",
                {"operation": capability, "authoritative": True, "source": "verifier"},
            )
            await store.complete_run(run.id, status=RunStatus.SUCCEEDED)
            return result, False, run.id
        except asyncio.CancelledError:
            await store.mark_attempt_unknown(attempt.id, error="cancelled")
            await store.require_manual_review(run.id, reason="auth operation cancelled before verification")
            raise
        except GeneratedAuthError as exc:
            failure = _generated_auth_failure(exc, capability)
            evidence = {
                "authoritative": True, "source": "verifier",
                "observation": "verifier_check", "observed_outcome": "failed",
                "attempt_id": attempt.id, "operation": capability,
                "error": failure["message"],
                "error_code": failure["code"],
                "error_status": failure["status"],
            }
            await store.finish_attempt(
                attempt.id, owner=owner, outcome="failed", evidence=evidence,
            )
            await store.finish_step(step.id, status=StepStatus.FAILED)
            await store.record_event(
                run.id, "AUTH_OPERATION_FAILED",
                {
                    "operation": capability,
                    "authoritative": True,
                    "source": "verifier",
                    "error_code": failure["code"],
                    "error_status": failure["status"],
                },
            )
            await store.complete_run(run.id, status=RunStatus.FAILED)
            raise HTTPException(failure["status"], failure["message"]) from exc
        except Exception:
            await store.mark_attempt_unknown(attempt.id, error="verifier interrupted")
            await store.require_manual_review(run.id, reason="auth verifier interrupted")
            raise HTTPException(503, "authentication operation requires manual review")
    except (DuplicateRequestError, LifecycleError, StaleWriterError) as exc:
        raise HTTPException(409, str(exc)) from exc


async def _checkpoint_operation(
    request: Request,
    *,
    workspace: str,
    capability: str,
    target: str,
    execute,
) -> tuple[dict[str, Any], bool, str]:
    owner = await _authenticate_flowdeck(request)
    try:
        canonical = await resolve_gateway_workspace(
            session_factory=get_session_factory(),
            user_id=owner,
            requested_workspace=workspace,
        )
    except AuthenticatedGatewayError as exc:
        raise HTTPException(403, str(exc)) from exc
    request_key = _request_key(request)
    store = DurableFlowDeck(get_session_factory())
    try:
        run, created = await store.create_run(
            request_key=request_key,
            owner=owner,
            workspace=canonical,
            step_name=f"checkpoint:{capability}",
        )
        if not created:
            operations = await store.get_run_operations(run.id)
            if operations:
                operation = operations[0]
                evidence = operation.authoritative_evidence or {}
                expected_capability = f"checkpoint.{capability}"
                if (
                    operation.capability != expected_capability
                    or operation.target != target
                ):
                    raise HTTPException(
                        409,
                        "Idempotency-Key is already used for a different checkpoint operation",
                    )
                if operation.status == OperationStatus.SUCCEEDED.value:
                    return dict(evidence.get("result") or {}), True, run.id
                if operation.status == OperationStatus.FAILED.value:
                    raise HTTPException(
                        int(evidence.get("error_status", 422)),
                        evidence.get("error", "checkpoint operation failed"),
                    )
                if run.status == RunStatus.CANCELLED.value:
                    raise HTTPException(409, "checkpoint operation was cancelled")
                raise HTTPException(409, "checkpoint operation requires recovery")
        await store.start_run(run.id)
        step = await store.get_step(run.id)
        await store.start_step(step.id)
        lease = await store.acquire_workspace_lease(
            workspace=canonical,
            run_id=run.id,
            owner=owner,
            ttl_ms=120_000,
        )
        if lease is None:
            await store.require_manual_review(
                run.id, reason="checkpoint workspace is already fenced by another run"
            )
            raise HTTPException(409, "checkpoint workspace is busy")
        operation, _ = await store.record_intent(
            run_id=run.id,
            step_id=step.id,
            idempotency_key=request_key,
            capability=f"checkpoint.{capability}",
            target=target,
            reconcile_kind="checkpoint",
        )
        attempt = await store.prepare_attempt(
            operation_id=operation.id,
            owner=owner,
            fencing_epoch=lease.epoch,
        )
        await store.record_event(
            run.id,
            "CHECKPOINT_OPERATION_STARTED",
            {"operation": capability, "attempt_id": attempt.id, "source": "runtime"},
        )
        checkpoint_tasks = getattr(request.app.state, "flowdeck_checkpoint_tasks", None)
        if checkpoint_tasks is None:
            checkpoint_tasks = {}
            request.app.state.flowdeck_checkpoint_tasks = checkpoint_tasks
        current_task = asyncio.current_task()
        if current_task is not None:
            checkpoint_tasks[run.id] = current_task
        try:
            result = execute(
                canonical,
                owner,
                run.id,
                lambda: store.assert_workspace_fence(
                    workspace=canonical,
                    run_id=run.id,
                    owner=owner,
                    epoch=lease.epoch,
                ),
            )
            if inspect.isawaitable(result):
                result = await result
            evidence = {
                "authoritative": True,
                "source": "verifier",
                "observation": "verifier_check",
                "observed_outcome": "succeeded",
                "attempt_id": attempt.id,
                "operation": capability,
                "result": result,
            }
            await store.finish_attempt(
                attempt.id,
                owner=owner,
                fencing_epoch=lease.epoch,
                outcome="succeeded",
                evidence=evidence,
            )
            await store.finish_step(step.id, status=StepStatus.SUCCEEDED)
            await store.record_event(
                run.id,
                "CHECKPOINT_OPERATION_VERIFIED",
                {"operation": capability, "source": "verifier"},
            )
            await store.complete_run(run.id, status=RunStatus.SUCCEEDED)
            await store.release_workspace_lease(
                workspace=canonical, owner=owner, epoch=lease.epoch
            )
            return result, False, run.id
        except asyncio.CancelledError:
            await store.mark_attempt_unknown(attempt.id, error="cancelled")
            current = await store.get_run(run.id)
            if current is None or current.status != RunStatus.CANCELLED.value:
                await store.require_manual_review(
                    run.id, reason="checkpoint operation cancelled before verification"
                )
            await store.release_workspace_lease(
                workspace=canonical, owner=owner, epoch=lease.epoch
            )
            raise HTTPException(
                503, "checkpoint operation was cancelled and requires reconciliation"
            )
        except CheckpointError as exc:
            outcome = "failed" if exc.code not in {"restore_unknown", "restore_stale"} else "unknown"
            if outcome == "unknown":
                await store.mark_attempt_unknown(attempt.id, error=exc.code)
                await store.require_manual_review(run.id, reason=exc.code)
                await store.release_workspace_lease(
                    workspace=canonical, owner=owner, epoch=lease.epoch
                )
                raise HTTPException(409, "checkpoint restore requires manual review") from exc
            evidence = {
                "authoritative": True,
                "source": "verifier",
                "observation": "verifier_check",
                "observed_outcome": "failed",
                "attempt_id": attempt.id,
                "operation": capability,
                "error": str(exc),
                "error_status": 422,
            }
            await store.finish_attempt(
                attempt.id,
                owner=owner,
                fencing_epoch=lease.epoch,
                outcome="failed",
                evidence=evidence,
            )
            await store.finish_step(step.id, status=StepStatus.FAILED)
            await store.complete_run(run.id, status=RunStatus.FAILED)
            await store.release_workspace_lease(
                workspace=canonical, owner=owner, epoch=lease.epoch
            )
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            await store.mark_attempt_unknown(attempt.id, error="checkpoint interrupted")
            await store.require_manual_review(run.id, reason="checkpoint interrupted")
            await store.release_workspace_lease(
                workspace=canonical, owner=owner, epoch=lease.epoch
            )
            raise HTTPException(503, "checkpoint operation requires manual review") from exc
        finally:
            if current_task is not None and checkpoint_tasks.get(run.id) is current_task:
                checkpoint_tasks.pop(run.id, None)
    except (DuplicateRequestError, LifecycleError, StaleWriterError) as exc:
        raise HTTPException(409, str(exc)) from exc


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


def _generated_auth_failure(exc: GeneratedAuthError, capability: str) -> dict[str, Any]:
    """Return failure metadata safe to expose and persist.

    Callback inputs include verifier settings and bearer-shaped values. Keep
    callback failures independent of exception text so a future adapter
    change cannot echo one of those inputs into durable state.
    """
    message = (
        "provider callback rejected"
        if capability == "callback.verify"
        else str(exc)
    )
    return {"message": message, "code": exc.code, "status": exc.status}


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
        adaptive = adaptive_route(body.objective, full_model_id)
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
        fresh_run_of = str(metadata.get("flowdeck_fresh_run_of") or "").strip()
        if fresh_run_of:
            await store.record_event(
                run.id,
                "RUN_FRESH_ATTEMPT_STARTED",
                {
                    "original_run_id": fresh_run_of,
                    "idempotency_boundary": "new_request_key",
                    "original_run_read_only": True,
                },
            )
        if hasattr(store, "record_event"):
            await store.record_event(
                run.id,
                "ADAPTIVE_ROUTE_SELECTED",
                {
                    "path": adaptive.path,
                    "rationale": adaptive.rationale,
                    "specialist_ids": list(adaptive.specialist_ids),
                    "execution": "native_cptr",
                    "model": full_model_id,
                },
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
                "flowdeck_route": adaptive.path,
            }
        )
        await Chat.update_meta(chat.id, chat_meta, now_ms())
        await ChatMessage.update(
            assistant_message.id,
            meta={
                "agent": "heidi",
                "flowdeck": True,
                "flowdeck_run_id": run.id,
                "flowdeck_route": adaptive.path,
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
        **(
            {"fresh_run": True, "original_run_id": fresh_run_of}
            if fresh_run_of
            else {}
        ),
        **({"audit": True, "reused": False} if audit_contract else {}),
    }


@router.post("/orchestrations", status_code=status.HTTP_200_OK)
async def create_orchestration(request: Request, body: OrchestrationRequest):
    """Run one controlled Heidi orchestration using server-owned policy."""
    return await _create_orchestration(request, body)


@router.post("/orchestrations/{run_id}/new-run", status_code=status.HTTP_200_OK)
async def create_new_orchestration_run(
    request: Request, run_id: str, body: NewRunRequest
):
    """Start a distinct attempt after a reviewer has inspected a terminal run.

    The original run is deliberately read-only here. A fresh Idempotency-Key
    creates a new durable run and the coordinator receives no authority to
    append events to the original run.
    """
    if run_id != body.original_run_id:
        raise HTTPException(409, "original run identity does not match the route")
    user_id, workspace, original = await _owned_run(request, run_id, body.workspace)
    if original.status not in {
        RunStatus.MANUAL_REVIEW_REQUIRED.value,
        RunStatus.ORPHANED.value,
    }:
        raise HTTPException(
            409,
            "a new FlowDeck run is only available for an unknown or manual-review outcome",
        )

    store = DurableFlowDeck(get_session_factory())
    original_events = await store.list_events(original.id)
    scope_event = next(
        (event for event in original_events if event.kind == "AUDIT_SCOPE_CREATED"),
        None,
    )
    metadata = dict(body.metadata or {})
    metadata["flowdeck_fresh_run_of"] = original.id
    metadata["source"] = "flowdeck-reviewer"
    audit_contract = None
    if scope_event:
        audit_contract = {
            "scope": scope_event.payload.get("scope") or {},
            "completion_contract": list(
                scope_event.payload.get("completion_contract") or []
            ),
        }
        if not audit_contract["scope"] or not audit_contract["completion_contract"]:
            raise HTTPException(409, "original audit evidence is incomplete")

    return await _create_orchestration(
        request,
        OrchestrationRequest(
            workspace=workspace,
            objective=body.objective,
            metadata=metadata,
        ),
        audit_contract=audit_contract,
    )


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
    from cptr.flowdeck.terminal_observer import recent_terminal_frames

    terminal_frames = recent_terminal_frames(run.id)
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
    response["events"].extend(
        {
            "id": f"{run.id}:terminal:{frame['sequence']}",
            "run_id": run.id,
            "sequence": frame["sequence"],
            "kind": "terminal_frame",
            "payload": frame,
            "created_at": frame["created_at"],
        }
        for frame in terminal_frames
    )
    response["events"].sort(key=lambda event: (event["sequence"], event["created_at"]))
    response["evidence_summary"] = build_audit_summary(
        response["events"],
        run_id=run.id,
        owner=run.owner,
    )
    return response


@router.get("/diagnostics/fdx-containment")
async def list_fdx_containment_diagnostics(
    request: Request,
    category: str | None = None,
    limit: int = 100,
):
    """List safe FDX containment diagnostics for the authenticated operator."""
    owner = await _authenticate_flowdeck(request)
    if category and category not in FDX_CONTAINMENT_CATEGORIES:
        raise HTTPException(400, "unsupported containment category")
    limit = max(1, min(limit, 200))
    async with get_session_factory()() as db:
        query = (
            select(FlowDeckEvent, FlowDeckRun)
            .join(FlowDeckRun, FlowDeckRun.id == FlowDeckEvent.run_id)
            .where(
                FlowDeckRun.owner == owner,
                FlowDeckEvent.kind == FDX_CONTAINMENT_EVENT_KIND,
            )
            .order_by(FlowDeckEvent.created_at.desc())
        )
        # Apply the requested category in SQL when possible, but keep the
        # exact payload validation below for both unfiltered and future data.
        if category:
            query = query.where(FlowDeckEvent.payload["category"].as_string() == category)
        query = query.limit(limit * _FDX_DIAGNOSTIC_SCAN_MULTIPLIER)
        rows = (await db.execute(query)).all()

    diagnostics = []
    for event, run in rows:
        event_category = safe_containment_category(event.payload)
        if event_category is None:
            continue
        if category and event_category != category:
            continue
        if (
            not isinstance(event.id, str)
            or not _SAFE_IDENTIFIER.fullmatch(event.id)
            or not isinstance(run.id, str)
            or not _SAFE_IDENTIFIER.fullmatch(run.id)
            or type(event.sequence) is not int
            or event.sequence < 0
            or type(event.created_at) is not int
            or event.created_at < 0
        ):
            continue
        run_status = str(run.status).lower()
        if run_status not in _SAFE_RUN_STATUSES:
            continue
        diagnostics.append(
            {
                "id": event.id,
                "run_id": run.id,
                "sequence": event.sequence,
                "category": event_category,
                "fallback": "native",
                "run_status": run_status,
                "run_outcome": "native_fallback",
                "created_at": event.created_at,
            }
        )
        if len(diagnostics) >= limit:
            break
    return {
        "categories": list(FDX_CONTAINMENT_CATEGORIES),
        "diagnostics": diagnostics,
        "total": len(diagnostics),
    }


@router.get("/orchestrations/{run_id}/evidence-report")
async def export_evidence_report(request: Request, run_id: str, workspace: str):
    """Download the bounded, redacted durable evidence summary for an owned run.

    Unlike the orchestration status response, an exported report must be
    reproducible by any API worker. Terminal observer frames are intentionally
    process-local and are therefore not part of this durable export.
    """
    owner, _, run = await _owned_run(request, run_id, workspace)
    store = DurableFlowDeck(get_session_factory())
    await store.record_evidence_report_export(run_id=run.id, owner=owner)
    events = await store.list_events(run.id)

    events = [
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
    summary = build_audit_summary(
        events,
        run_id=run.id,
        owner=run.owner,
    )
    return Response(
        content=json.dumps(summary, sort_keys=True, separators=(",", ":")),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="flowdeck-evidence-{run.id[:8]}.json"'
            ),
            "Cache-Control": "no-store",
        },
    )


@router.get("/generated-auth/config")
async def generated_auth_config(request: Request, workspace: str):
    _same_origin(request)
    _, service = await _generated_auth_service(request, workspace)
    return service.metadata()


@router.post("/checkpoints/capture")
async def capture_checkpoint(request: Request, body: CheckpointRequest):
    _same_origin(request)
    result, reused, run_id = await _checkpoint_operation(
        request,
        workspace=body.workspace,
        capability="capture",
        target="workspace-head",
        execute=lambda canonical, owner, run_id, fence=None: CheckpointService(
            get_session_factory()
        ).capture(
            workspace=canonical,
            owner=owner,
            run_id=run_id,
            assert_fence=fence,
        ),
    )
    return {**result, "run_id": run_id, "reused": reused}


@router.get("/checkpoints")
async def list_checkpoints(request: Request, workspace: str):
    _same_origin(request)
    owner = await _authenticate_flowdeck(request)
    try:
        canonical = await resolve_gateway_workspace(
            session_factory=get_session_factory(),
            user_id=owner,
            requested_workspace=workspace,
        )
    except AuthenticatedGatewayError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {
        "checkpoints": await CheckpointService(get_session_factory()).list(
            workspace=canonical, owner=owner
        )
    }


@router.post("/checkpoints/restore")
async def restore_checkpoint(request: Request, body: CheckpointRestoreRequest):
    _same_origin(request)
    result, reused, run_id = await _checkpoint_operation(
        request,
        workspace=body.workspace,
        capability="restore",
        target=body.checkpoint_id,
        execute=lambda canonical, owner, run_id, fence=None: CheckpointService(
            get_session_factory()
        ).restore(
            checkpoint_id=body.checkpoint_id,
            workspace=canonical,
            owner=owner,
            assert_fence=fence,
        ),
    )
    return {**result, "run_id": run_id, "reused": reused}


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
    if not hmac.compare_digest(body.csrf, request.cookies.get(CSRF_COOKIE, "")):
        raise HTTPException(403, "CSRF validation failed")
    result, reused, run_id = await _generated_auth_operation(
        request, workspace=body.workspace, capability="signup",
        target=body.email.strip().lower(),
        execute=lambda service: {
            "user": _auth_user_payload(service.signup(body.email, body.password)),
            "provider": service.provider.value,
        },
    )
    return {**result, "run_id": run_id, "reused": reused}


@router.post("/generated-auth/signin")
async def generated_auth_signin(request: Request, body: GeneratedAuthCredentials, response: Response):
    _same_origin(request)
    if not hmac.compare_digest(body.csrf, request.cookies.get(CSRF_COOKIE, "")):
        raise HTTPException(403, "CSRF validation failed")
    result, reused, run_id = await _generated_auth_operation(
        request, workspace=body.workspace, capability="signin",
        target=body.email.strip().lower(),
        execute=lambda service: (
            lambda signed: {
                "user": _auth_user_payload(signed[0]), "expires_at": signed[3],
                "provider": service.provider.value, "_session": signed[1], "_csrf": signed[2],
            }
        )(service.signin(body.email, body.password)),
    )
    if not reused and result.get("_session"):
        response.set_cookie(
            SESSION_COOKIE, result["_session"], max_age=SESSION_TTL_SECONDS,
            httponly=True, secure=request.url.scheme == "https", samesite="strict",
        )
        response.set_cookie(
            CSRF_COOKIE, result["_csrf"], max_age=CSRF_TTL_SECONDS,
            httponly=False, secure=request.url.scheme == "https", samesite="strict",
        )
    return {key: value for key, value in {**result, "run_id": run_id, "reused": reused}.items()
            if not key.startswith("_")}


@router.get("/generated-auth/session")
async def generated_auth_session(request: Request, workspace: str):
    _same_origin(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    key = request.headers.get("Idempotency-Key") or (
        "generated-auth-session-" + hashlib.sha256(token.encode()).hexdigest()[:32]
    )
    request.scope.setdefault("headers", [])
    # The helper intentionally validates the same key namespace as mutations.
    original = request.headers.get("Idempotency-Key")
    if not original:
        request.scope["headers"].append((b"idempotency-key", key.encode()))
    try:
        result, reused, run_id = await _generated_auth_operation(
            request, workspace=workspace, capability="inspect",
            target="current-session",
            execute=lambda service: (
                lambda session: {"user": _auth_user_payload(session.user), "expires_at": session.expires_at}
            )(service.session(token)),
        )
    finally:
        if not original:
            request.scope["headers"] = [
                item for item in request.scope["headers"] if item[0].lower() != b"idempotency-key"
            ]
    return {**result, "run_id": run_id, "reused": reused}


@router.get("/generated-auth/operations/{run_id}")
async def generated_auth_operation_status(request: Request, run_id: str, workspace: str):
    _same_origin(request)
    _, canonical, run = await _owned_run(request, run_id, workspace)
    if run.workspace != canonical:
        raise HTTPException(404, "authentication operation not found")
    store = DurableFlowDeck(get_session_factory())
    operations = await store.get_run_operations(run.id)
    events = await store.list_events(run.id)
    return {
        **_safe_run(run),
        "operations": [
            {
                "id": operation.id,
                "capability": operation.capability,
                "status": operation.status.lower(),
                "outcome": operation.outcome,
                "evidence": operation.authoritative_evidence,
            }
            for operation in operations
        ],
        "events": [
            {"kind": event.kind, "payload": event.payload, "created_at": event.created_at}
            for event in events
        ],
    }


@router.post("/generated-auth/operations/{run_id}/cancel")
async def cancel_generated_auth_operation(request: Request, run_id: str, workspace: str):
    _same_origin(request)
    user_id, canonical, run = await _owned_run(request, run_id, workspace)
    store = DurableFlowDeck(get_session_factory())
    try:
        cancelled = await store.cancel_run(
            run_id=run.id, owner=user_id, workspace=canonical,
        )
    except LifecycleError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _safe_run(cancelled)


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
    if not request.cookies.get(SESSION_COOKIE):
        raise HTTPException(401, "authentication required")
    result, reused, run_id = await _generated_auth_operation(
        request, workspace=body.workspace, capability="signout",
        target="current-session",
        execute=lambda service: (
            service.signout(request.cookies.get(SESSION_COOKIE, ""), request.cookies.get(CSRF_COOKIE, "")),
            {"ok": True},
        )[1],
    )
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return {**result, "run_id": run_id, "reused": reused}


@router.post("/generated-auth/callback/verify")
async def generated_auth_callback(
    request: Request,
    body: GeneratedAuthCallback = Depends(_parse_generated_auth_callback),
):
    _same_origin(request)
    result, reused, run_id = await _generated_auth_operation(
        request, workspace=body.workspace, capability="callback.verify",
        # Never persist caller-provided verifier settings in the operation
        # identity or its fingerprint. The service still validates them
        # against the server-owned verifier configuration below.
        target="external-callback",
        execute=lambda service: (
            service.verify_external_callback(
                issuer=body.issuer, audience=body.audience, redirect_uri=body.redirect_uri,
                state=body.state, expected_state=request.cookies.get(CSRF_COOKIE, ""),
                nonce=body.nonce, expected_nonce=request.cookies.get(CSRF_COOKIE, ""),
                code_verifier=body.code_verifier,
            ),
            {"verified": True, "provider": service.provider.value},
        )[1],
    )
    return {**result, "run_id": run_id, "reused": reused}


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
    checkpoint_tasks = getattr(request.app.state, "flowdeck_checkpoint_tasks", {})
    checkpoint_task = checkpoint_tasks.get(run.id)
    if checkpoint_task is not None and checkpoint_task is not asyncio.current_task():
        checkpoint_task.cancel()
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
