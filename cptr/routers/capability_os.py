"""Authenticated six-operation Control API for Capability OS."""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from cptr.env import (
    CAPABILITY_OS_BUILD_MAX_CPU_MILLIS,
    CAPABILITY_OS_BUILD_MAX_DISK_MIB,
    CAPABILITY_OS_BUILD_MAX_MEMORY_MIB,
    CAPABILITY_OS_BUILD_MAX_OUTPUT_BYTES,
    CAPABILITY_OS_BUILD_MAX_PIDS,
    CAPABILITY_OS_BUILD_MAX_WALL_TIME_MS,
    CAPABILITY_OS_SANDBOX_SOCKET,
    DATA_DIR,
)
from cptr.services.capability_os.authority import AuthorityBroker, AuthorityDenied
from cptr.services.capability_os.compiler import CapabilityCompileError, CapabilityCompiler
from cptr.services.capability_os.control import CapabilityOsControlService, CapabilityOsUnavailable
from cptr.services.capability_os.credential_broker import ConfigCredentialProvider, CredentialBroker
from cptr.services.capability_os.contracts import CapabilityRequest
from cptr.services.capability_os.evidence import EvidenceService, EvidenceViolation
from cptr.services.capability_os.evolution import EvolutionGate
from cptr.services.capability_os.forge import ContentAddressedBlobStore, ToolForge
from cptr.services.capability_os.mcp_fabric import McpFabric
from cptr.services.capability_os.mcp_package import McpbPackagePreparer
from cptr.services.capability_os.mcp_remote import McpAcquisitionService, StreamableHttpMcpConnector
from cptr.services.capability_os.native_executor import NativeActionExecutor
from cptr.services.capability_os.policy import (
    CompositeAuthorityPolicyProvider,
    ConfigStandingAuthorityPolicyProvider,
    SafeIsolationAuthorityPolicyProvider,
)
from cptr.services.capability_os.resolver import CapabilityResolver
from cptr.services.capability_os.runtime import RuntimeBroker, RuntimeUnavailable
from cptr.services.capability_os.sandbox_adapter import BrokerToolBuilder, BrokerToolRunner
from cptr.services.capability_os.sandbox_broker import SandboxBrokerClient
from cptr.services.capability_os.skill_forge import SkillMcpActivator
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.services.capability_os.tasks import CapabilityTaskCoordinator, CapabilityTaskNotExecutable, CapabilityTaskNotFound
from cptr.services.control_auth import require_control_user
from cptr.services.factory_discovery import FactoryDiscovery, QuarantineCache, SafeHttpArtifactFetcher
from cptr.services.factory_discovery_providers.mcp_registry import McpRegistryDiscoveryProvider

capability_os_router = APIRouter(prefix="/api/control/v1/capability-os", tags=["control", "capability-os"])
router = capability_os_router


class ResolveRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=200)
    required: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    optional: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    forbidden: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class OperationRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=200)
    operation: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)


class ExecuteRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=200)
    capability_digest: str = Field(min_length=1, max_length=200)
    lease_id: str | None = Field(default=None, min_length=1, max_length=200)
    spec: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = Field(default=None, max_length=200)


class ReflectRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=200)
    claims: dict[str, Any] = Field(default_factory=dict)
    artifact_digest: str | None = Field(default=None, max_length=200)
    lease_id: str | None = Field(default=None, max_length=200)
    comparison: dict[str, Any] | None = None
    change_class: str | None = Field(default=None, max_length=80)
    promotion_target_state: str | None = Field(default=None, max_length=80)
    owner_approval_id: str | None = Field(default=None, max_length=200)


async def _user(request: Request, scope: str) -> str:
    return await require_control_user(request, scope)


def _default_tool_builder() -> BrokerToolBuilder:
    return BrokerToolBuilder(
        client=SandboxBrokerClient(socket_path=CAPABILITY_OS_SANDBOX_SOCKET)
    )


def _default_tool_runner() -> BrokerToolRunner:
    return BrokerToolRunner(
        client=SandboxBrokerClient(socket_path=CAPABILITY_OS_SANDBOX_SOCKET)
    )


def _default_credential_broker(*, clock_ms=None, lease_validator=None) -> CredentialBroker:
    return CredentialBroker(
        provider=ConfigCredentialProvider(),
        clock_ms=clock_ms or (lambda: int(time.time() * 1000)),
        lease_validator=lease_validator,
    )


def _default_policy_provider(*, config_getter=None) -> CompositeAuthorityPolicyProvider:
    return CompositeAuthorityPolicyProvider((
        SafeIsolationAuthorityPolicyProvider(
            max_cpu_millis=CAPABILITY_OS_BUILD_MAX_CPU_MILLIS,
            max_memory_mib=CAPABILITY_OS_BUILD_MAX_MEMORY_MIB,
            max_disk_mib=CAPABILITY_OS_BUILD_MAX_DISK_MIB,
            max_pids=CAPABILITY_OS_BUILD_MAX_PIDS,
            max_wall_time_ms=CAPABILITY_OS_BUILD_MAX_WALL_TIME_MS,
            max_output_bytes=CAPABILITY_OS_BUILD_MAX_OUTPUT_BYTES,
        ),
        ConfigStandingAuthorityPolicyProvider(config_getter=config_getter),
    ))


def _service(request: Request) -> CapabilityOsControlService:
    service = getattr(request.app.state, "capability_os_control_service", None)
    if service is not None:
        return service
    def clock() -> int:
        return int(time.time() * 1000)

    store = SqlCapabilityOsStore()
    authority = AuthorityBroker(
        store=store,
        clock_ms=clock,
        approval_verifier=getattr(request.app.state, "capability_os_approval_verifier", None),
    )
    runtime = RuntimeBroker(production=True)
    tool_builder = getattr(request.app.state, "capability_os_tool_builder", None)
    if tool_builder is None:
        tool_builder = _default_tool_builder()
    tool_runner = getattr(request.app.state, "capability_os_tool_runner", None)
    if tool_runner is None:
        tool_runner = _default_tool_runner()
    policy_provider = getattr(request.app.state, "capability_os_policy_provider", None)
    if policy_provider is None:
        policy_provider = _default_policy_provider()
    action_executor = getattr(request.app.state, "capability_os_action_executor", None)
    if action_executor is None:
        action_executor = NativeActionExecutor(clock_ms=clock)
    credential_broker = getattr(request.app.state, "capability_os_credential_broker", None)
    if credential_broker is None:
        async def validate_credential_lease(lease):
            try:
                await authority.require_active(
                    lease.lease_id,
                    task_id=lease.task_id,
                    artifact_digest=lease.artifact_digest,
                    runtime_profile=lease.runtime_profile,
                    credential_names=tuple(
                        str(item)
                        for item in lease.credentials.get("logicalNames") or ()
                        if str(item)
                    ),
                )
            except AuthorityDenied:
                return False
            return True

        credential_broker = _default_credential_broker(
            clock_ms=clock,
            lease_validator=validate_credential_lease,
        )
    fabric = McpFabric(
        store=store,
        authority=authority,
        credential_broker=credential_broker,
        clock_ms=clock,
    )
    skill_activator = getattr(request.app.state, "capability_os_skill_activator", None)
    if skill_activator is None:
        skill_activator = SkillMcpActivator(store=store, authority=authority, clock_ms=clock)
    mcp_connector = getattr(request.app.state, "capability_os_mcp_connector", None)
    if mcp_connector is None:
        mcp_connector = StreamableHttpMcpConnector()
    blobs = ContentAddressedBlobStore(DATA_DIR / "capability-os" / "blobs")
    mcp_acquisition = getattr(request.app.state, "capability_os_mcp_acquisition", None)
    if mcp_acquisition is None:
        package_resources = {
            "cpuMillis": CAPABILITY_OS_BUILD_MAX_CPU_MILLIS,
            "memoryMiB": CAPABILITY_OS_BUILD_MAX_MEMORY_MIB,
            "diskMiB": CAPABILITY_OS_BUILD_MAX_DISK_MIB,
            "pids": CAPABILITY_OS_BUILD_MAX_PIDS,
            "wallTimeMs": CAPABILITY_OS_BUILD_MAX_WALL_TIME_MS,
            "maxOutputBytes": CAPABILITY_OS_BUILD_MAX_OUTPUT_BYTES,
        }
        mcp_acquisition = McpAcquisitionService(
            store=store,
            fabric=fabric,
            discovery=FactoryDiscovery(
                providers=(McpRegistryDiscoveryProvider(),),
                artifact_fetcher=SafeHttpArtifactFetcher(),
                quarantine_cache=QuarantineCache(DATA_DIR / "capability-os" / "mcp-quarantine"),
            ),
            connector=mcp_connector,
            package_preparer=McpbPackagePreparer(blobs=blobs),
            package_runner=tool_runner,
            package_resources=package_resources,
            clock_ms=clock,
        )
    service = CapabilityOsControlService(
        store=store,
        tasks=CapabilityTaskCoordinator(),
        authority=authority,
        resolver=CapabilityResolver(store=store),
        forge=ToolForge(store=store, blobs=blobs,
                        runtime=runtime, builder=tool_builder, runner=tool_runner,
                        clock_ms=clock),
        compiler=CapabilityCompiler(),
        evidence=EvidenceService(store=store, clock_ms=clock),
        evolution=EvolutionGate(),
        runtime=runtime,
        fabric=fabric,
        action_executor=action_executor,
        mcp_connector=mcp_connector,
        mcp_acquisition=mcp_acquisition,
        credential_broker=credential_broker,
        skill_activator=skill_activator,
        evolution_approval_verifier=getattr(
            request.app.state, "capability_os_evolution_approval_verifier", None
        ),
        policy_provider=policy_provider,
        clock_ms=clock,
    )
    request.app.state.capability_os_control_service = service
    return service


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (CapabilityTaskNotFound, KeyError)):
        return HTTPException(404, {"code": "CAPABILITY_OS_NOT_FOUND", "message": str(exc)})
    if isinstance(exc, CapabilityTaskNotExecutable):
        return HTTPException(409, {"code": "CAPABILITY_OS_TASK_NOT_EXECUTABLE", "message": str(exc)})
    if isinstance(exc, (AuthorityDenied, PermissionError)):
        return HTTPException(403, {"code": "CAPABILITY_OS_AUTHORITY_DENIED", "message": str(exc)})
    if isinstance(exc, (CapabilityOsUnavailable, RuntimeUnavailable)):
        return HTTPException(503, {"code": "CAPABILITY_OS_RUNTIME_UNAVAILABLE", "message": str(exc)})
    if isinstance(exc, (CapabilityCompileError, EvidenceViolation, ValueError, TypeError)):
        return HTTPException(422, {"code": "CAPABILITY_OS_INVALID_REQUEST", "message": str(exc)})
    return HTTPException(500, {"code": "CAPABILITY_OS_FAILED", "message": "Capability OS operation failed"})


def _reqs(values: list[dict[str, Any]]) -> tuple[CapabilityRequest, ...]:
    return tuple(CapabilityRequest.from_dict(item) for item in values)


@capability_os_router.get("/inspect")
async def inspect_capability_os(request: Request, task_id: str = Query(min_length=1, max_length=200),
                                artifact_digest: str | None = Query(default=None, max_length=200),
                                limit: int = Query(default=50, ge=1, le=100)):
    user_id = await _user(request, "capability:read")
    try:
        return await _service(request).inspect(user_id=user_id, task_id=task_id,
                                               artifact_digest=artifact_digest, limit=limit)
    except Exception as exc:
        raise _error(exc) from exc


@capability_os_router.post("/resolve")
async def resolve_capability_os(request: Request, body: ResolveRequest):
    user_id = await _user(request, "capability:read")
    try:
        return await _service(request).resolve(user_id=user_id, task_id=body.task_id,
                                               required=_reqs(body.required), optional=_reqs(body.optional),
                                               forbidden=_reqs(body.forbidden))
    except Exception as exc:
        raise _error(exc) from exc


@capability_os_router.post("/forge")
async def forge_capability_os(request: Request, body: OperationRequest):
    user_id = await _user(request, "capability:write")
    try:
        return await _service(request).forge(user_id=user_id, task_id=body.task_id,
                                             operation=body.operation, payload=body.payload)
    except Exception as exc:
        raise _error(exc) from exc


@capability_os_router.post("/execute")
async def execute_capability_os(request: Request, body: ExecuteRequest):
    user_id = await _user(request, "capability:execute")
    try:
        return await _service(request).execute(user_id=user_id, task_id=body.task_id,
                                               capability_digest=body.capability_digest, lease_id=body.lease_id,
                                               spec=body.spec, inputs=body.inputs, approval_id=body.approval_id)
    except Exception as exc:
        raise _error(exc) from exc


@capability_os_router.post("/acquire")
async def acquire_capability_os(request: Request, body: OperationRequest):
    user_id = await _user(request, "capability:execute")
    try:
        return await _service(request).acquire(user_id=user_id, task_id=body.task_id,
                                               operation=body.operation, payload=body.payload)
    except Exception as exc:
        raise _error(exc) from exc


@capability_os_router.post("/reflect")
async def reflect_capability_os(request: Request, body: ReflectRequest):
    user_id = await _user(request, "capability:write")
    try:
        return await _service(request).reflect(user_id=user_id, task_id=body.task_id, kind=body.kind,
                                               claims=body.claims, artifact_digest=body.artifact_digest,
                                               lease_id=body.lease_id, comparison=body.comparison,
                                               change_class=body.change_class,
                                               promotion_target_state=body.promotion_target_state,
                                               owner_approval_id=body.owner_approval_id)
    except Exception as exc:
        raise _error(exc) from exc
