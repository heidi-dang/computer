"""Server-authoritative remote MCP discovery, qualification, and projected execution.

Remote MCP metadata is never accepted as a trust claim from the caller. The
server discovers registry candidates, validates public HTTPS endpoints, performs
an MCP handshake/tool enumeration with the official SDK, persists the observed
identity as a task/user-scoped ``McpAdapter`` artifact, and revalidates that
identity before mount and invocation.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from cptr.services.capability_os.authority import AuthorityBroker, CapabilityLease
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    CapabilityRequest,
    canonical_json,
    create_artifact,
    digest_payload,
)
from cptr.services.capability_os.mcp_fabric import AcquisitionGoal, McpCandidate, McpFabric
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.services.capability_os.vm import ActionResult
from cptr.services.factory_discovery import (
    DiscoveryBudget,
    DiscoveryBudgetExceeded,
    DiscoveryCandidate,
    FactoryDiscovery,
    ResearchSignals,
)


class RemoteMcpError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteMcpTool:
    name: str
    input_schema_digest: str


@dataclass(frozen=True)
class RemoteMcpObservation:
    remote_url: str
    protocol_version: str
    server_name: str
    server_version: str
    tools: tuple[RemoteMcpTool, ...]

    @property
    def fingerprint(self) -> str:
        return digest_payload(
            {
                "remoteUrl": self.remote_url,
                "protocolVersion": self.protocol_version,
                "serverName": self.server_name,
                "serverVersion": self.server_version,
                "tools": [
                    {"name": tool.name, "inputSchemaDigest": tool.input_schema_digest}
                    for tool in self.tools
                ],
            }
        )


@dataclass(frozen=True)
class AcquiredMcpAdapter:
    artifact_digest: str
    server_id: str
    version: str
    state: str
    eligible: bool
    reasons: tuple[str, ...]
    projected_match: tuple[str, ...]


class PublicHttpsEndpointValidator:
    """Reject local/private MCP endpoints and normalize public HTTPS URLs."""

    @staticmethod
    def _is_public_ip(value: str) -> bool:
        address = ipaddress.ip_address(value)
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )

    async def validate(self, raw_url: str) -> str:
        parsed = urlsplit(str(raw_url or "").strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise RemoteMcpError("remote MCP endpoint must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise RemoteMcpError("remote MCP endpoint must not contain userinfo")
        if parsed.fragment:
            raise RemoteMcpError("remote MCP endpoint must not contain a fragment")
        host = parsed.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost"):
            raise RemoteMcpError("remote MCP endpoint must not target localhost")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not self._is_public_ip(str(literal)):
                raise RemoteMcpError("remote MCP endpoint must target a public address")
        else:
            try:
                addresses = await asyncio.to_thread(
                    socket.getaddrinfo,
                    host,
                    parsed.port or 443,
                    0,
                    socket.SOCK_STREAM,
                )
            except OSError as exc:
                raise RemoteMcpError("remote MCP endpoint could not be resolved") from exc
            resolved = {str(entry[4][0]) for entry in addresses if entry and entry[4]}
            if not resolved or any(not self._is_public_ip(address) for address in resolved):
                raise RemoteMcpError("remote MCP endpoint DNS resolved to a non-public address")
        netloc = host
        if ":" in host and not host.startswith("["):
            netloc = f"[{host}]"
        if parsed.port is not None and parsed.port != 443:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


class StreamableHttpMcpConnector:
    """Fresh-session Streamable HTTP connector backed by the official MCP SDK.

    No caller-supplied headers, credentials, tool lists, or trust booleans are
    accepted. Each probe/invocation validates the endpoint again and uses a
    fresh SDK session, avoiding durable hidden network authority in the server.
    """

    def __init__(
        self,
        *,
        endpoint_validator: PublicHttpsEndpointValidator | None = None,
        timeout_seconds: float = 15.0,
        max_tools: int = 128,
        max_tool_pages: int = 8,
        max_result_bytes: int = 1_048_576,
    ) -> None:
        if timeout_seconds <= 0 or max_tools <= 0 or max_tool_pages <= 0 or max_result_bytes <= 0:
            raise ValueError("remote MCP connector bounds must be positive")
        self._validator = endpoint_validator or PublicHttpsEndpointValidator()
        self._timeout_seconds = float(timeout_seconds)
        self._max_tools = int(max_tools)
        self._max_tool_pages = int(max_tool_pages)
        self._max_result_bytes = int(max_result_bytes)

    @staticmethod
    def _sdk():
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except Exception as exc:  # pragma: no cover - environment-specific missing optional dependency
            raise RemoteMcpError("official MCP SDK is not installed") from exc
        return ClientSession, streamable_http_client

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "cptr-capability-os", "Accept": "application/json, text/event-stream"},
        )

    @staticmethod
    def _tool_descriptor(tool: Any) -> RemoteMcpTool:
        name = str(getattr(tool, "name", "") or "").strip()
        if not name or len(name) > 256 or any(ord(char) < 32 for char in name):
            raise RemoteMcpError("remote MCP returned an invalid tool name")
        schema = getattr(tool, "inputSchema", None)
        if not isinstance(schema, dict):
            raise RemoteMcpError("remote MCP returned an invalid tool schema")
        try:
            schema_digest = digest_payload(schema)
        except (TypeError, ValueError) as exc:
            raise RemoteMcpError("remote MCP returned a non-canonical tool schema") from exc
        return RemoteMcpTool(name=name, input_schema_digest=schema_digest)

    async def probe(self, remote_url: str) -> RemoteMcpObservation:
        url = await self._validator.validate(remote_url)
        ClientSession, streamable_http_client = self._sdk()
        tools: list[RemoteMcpTool] = []
        seen_names: set[str] = set()
        async with self._http_client() as http_client:
            try:
                async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        initialized = await asyncio.wait_for(
                            session.initialize(), timeout=self._timeout_seconds
                        )
                        cursor = None
                        for _page in range(self._max_tool_pages):
                            result = await asyncio.wait_for(
                                session.list_tools(cursor=cursor), timeout=self._timeout_seconds
                            )
                            for raw_tool in list(getattr(result, "tools", ()) or ()):
                                descriptor = self._tool_descriptor(raw_tool)
                                if descriptor.name in seen_names:
                                    raise RemoteMcpError("remote MCP returned duplicate tool names")
                                seen_names.add(descriptor.name)
                                tools.append(descriptor)
                                if len(tools) > self._max_tools:
                                    raise RemoteMcpError("remote MCP exceeds the tool-count bound")
                            cursor = getattr(result, "nextCursor", None)
                            if not cursor:
                                break
                        else:
                            if cursor:
                                raise RemoteMcpError("remote MCP tool pagination exceeds the page bound")
            except RemoteMcpError:
                raise
            except Exception as exc:
                raise RemoteMcpError(f"remote MCP handshake failed: {exc.__class__.__name__}") from exc

        info = getattr(initialized, "serverInfo", None)
        server_name = str(getattr(info, "name", "") or "unknown")[:256]
        server_version = str(getattr(info, "version", "") or "unknown")[:128]
        protocol_version = str(getattr(initialized, "protocolVersion", "") or "unknown")[:128]
        return RemoteMcpObservation(
            remote_url=url,
            protocol_version=protocol_version,
            server_name=server_name,
            server_version=server_version,
            tools=tuple(tools),
        )

    async def invoke(self, *, remote_url: str, tool_name: str, arguments: dict[str, Any]) -> ActionResult:
        url = await self._validator.validate(remote_url)
        if not isinstance(arguments, dict):
            raise RemoteMcpError("remote MCP tool arguments must be an object")
        ClientSession, streamable_http_client = self._sdk()
        async with self._http_client() as http_client:
            try:
                async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(session.initialize(), timeout=self._timeout_seconds)
                        result = await asyncio.wait_for(
                            session.call_tool(tool_name, arguments=arguments),
                            timeout=self._timeout_seconds,
                        )
            except Exception as exc:
                raise RemoteMcpError(f"remote MCP invocation failed: {exc.__class__.__name__}") from exc

        if hasattr(result, "model_dump"):
            payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        else:  # defensive compatibility path
            payload = {
                "isError": bool(getattr(result, "isError", False)),
                "structuredContent": getattr(result, "structuredContent", None),
            }
        try:
            encoded = canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise RemoteMcpError("remote MCP returned a non-canonical result") from exc
        if len(encoded) > self._max_result_bytes:
            raise RemoteMcpError("remote MCP result exceeds the response-size bound")
        return ActionResult(
            output=json.loads(encoded.decode("utf-8")),
            verification_passed=not bool(getattr(result, "isError", False)),
            metadata={"transport": "streamable-http", "remoteUrl": url, "tool": tool_name},
        )

    async def release(self, **_kwargs) -> None:
        # Sessions are intentionally fresh and non-durable; the authority lease
        # and persisted mount are the lifecycle state. There is no hidden socket
        # or client session to keep alive after a request.
        return None


class McpAcquisitionService:
    def __init__(
        self,
        *,
        store: SqlCapabilityOsStore,
        fabric: McpFabric,
        discovery: FactoryDiscovery,
        connector: StreamableHttpMcpConnector,
        clock_ms=lambda: int(time.time() * 1000),
        max_candidates: int = 8,
    ) -> None:
        self._store = store
        self._fabric = fabric
        self._discovery = discovery
        self._connector = connector
        self._clock_ms = clock_ms
        self._max_candidates = max(1, min(int(max_candidates), 32))

    @staticmethod
    def _created_at(now_ms: int) -> str:
        return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _remote_entries(candidate: DiscoveryCandidate) -> tuple[str, ...]:
        remotes = candidate.metadata.get("remotes") if isinstance(candidate.metadata, dict) else None
        if not isinstance(remotes, list):
            return ()
        values: list[str] = []
        for item in remotes[:20]:
            if not isinstance(item, dict) or item.get("type") != "streamable-http":
                continue
            url = str(item.get("url") or "").strip()
            if url:
                values.append(url)
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _permission(server_id: str) -> CapabilityRequest:
        return CapabilityRequest("mcp.invoke", f"mcp:{server_id}/*")

    @staticmethod
    def _candidate_from_artifact(row) -> McpCandidate:
        spec = dict(row.spec or {})
        remote = spec.get("remote") if isinstance(spec.get("remote"), dict) else {}
        tool_rows = spec.get("tools") if isinstance(spec.get("tools"), list) else []
        tools = tuple(
            str(item.get("name") or "")
            for item in tool_rows
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        )
        server_id = str(spec.get("serverId") or "").strip()
        permission_rows = spec.get("permissions") if isinstance(spec.get("permissions"), list) else []
        permissions = tuple(
            CapabilityRequest.from_dict(item) for item in permission_rows if isinstance(item, dict)
        )
        return McpCandidate(
            server_id=server_id,
            version=str(row.version),
            digest=str(row.content_digest),
            transport_kind=str(remote.get("transport") or ""),
            tools=tools,
            permissions=permissions,
            identity_ok=True,
            auth_ok=True,
            sandboxable=True,
            hard_denies=(),
        )

    async def discover_and_qualify(
        self,
        *,
        user_id: str,
        task_id: str,
        goal: AcquisitionGoal,
        query: str,
    ) -> list[AcquiredMcpAdapter]:
        batch = await self._discovery.discover(
            query,
            signals=ResearchSignals(unfamiliar_technology=True, api_uncertain=True),
            budget=DiscoveryBudget(
                max_providers=1,
                max_results=self._max_candidates,
                max_bytes=512_000,
                max_runtime_ms=20_000,
            ),
        )
        results: list[AcquiredMcpAdapter] = []
        for discovered in batch.candidates[: self._max_candidates]:
            if discovered.provider != "mcp_registry" or discovered.candidate_type != "mcp_server":
                continue
            remotes = self._remote_entries(discovered)
            if remotes:
                for remote_url in remotes:
                    try:
                        observation = await self._connector.probe(remote_url)
                    except Exception as exc:
                        results.append(
                            AcquiredMcpAdapter(
                                artifact_digest="",
                                server_id=discovered.name,
                                version=discovered.version or "unversioned",
                                state="rejected",
                                eligible=False,
                                reasons=(f"remote-probe:{exc.__class__.__name__}",),
                                projected_match=(),
                            )
                        )
                        continue
                    tool_names = tuple(tool.name for tool in observation.tools)
                    required = tuple(dict.fromkeys(x.strip() for x in goal.required if x.strip()))
                    missing = tuple(item for item in required if item not in set(tool_names))
                    forbidden = set(item.strip() for item in goal.forbidden if item.strip())
                    required_forbidden = tuple(item for item in required if item in forbidden)
                    reasons = tuple(
                        [*(f"missing-tool:{item}" for item in missing),
                         *(f"forbidden-tool:{item}" for item in required_forbidden)]
                    )
                    permission = self._permission(discovered.name)
                    spec = {
                        "serverId": discovered.name,
                        "acquisitionTaskId": task_id,
                        "registryStableId": discovered.stable_id,
                        "registryIdentity": discovered.identity,
                        "remote": {"url": observation.remote_url, "transport": "streamable-http"},
                        "protocolVersion": observation.protocol_version,
                        "serverInfo": {
                            "name": observation.server_name,
                            "version": observation.server_version,
                        },
                        "identityFingerprint": observation.fingerprint,
                        "tools": [
                            {"name": tool.name, "inputSchemaDigest": tool.input_schema_digest}
                            for tool in observation.tools
                        ],
                        "permissions": [permission.to_dict()],
                        "qualification": {
                            "identity": "registry+live-handshake",
                            "auth": "live-handshake",
                            "sandbox": "remote-no-local-execution",
                        },
                    }
                    artifact = create_artifact(
                        artifact_id=f"mcp.adapter.{hashlib.sha256((discovered.name + observation.remote_url).encode()).hexdigest()[:24]}",
                        version=discovered.version or observation.server_version or "unversioned",
                        kind=ArtifactKind.MCP_ADAPTER,
                        owner=ArtifactOwner.EXTERNAL,
                        origin=ArtifactOrigin.MCP,
                        spec=spec,
                        created_at=self._created_at(int(self._clock_ms())),
                        user_id=user_id,
                        task_origin=task_id,
                        source_digest=digest_payload(discovered.to_dict()),
                        state=ArtifactState.QUALIFIED if not reasons else ArtifactState.EPHEMERAL,
                    )
                    row = await self._store.persist_artifact(artifact)
                    results.append(
                        AcquiredMcpAdapter(
                            artifact_digest=row.content_digest,
                            server_id=discovered.name,
                            version=row.version,
                            state=row.state,
                            eligible=not reasons,
                            reasons=reasons,
                            projected_match=tuple(item for item in required if item in set(tool_names)),
                        )
                    )
                continue

            # Packaged MCPs remain data-only until the gVisor qualification gate
            # is promoted. Fetching is bounded and cannot execute the package.
            if discovered.source_uri:
                reasons: tuple[str, ...] = ("package-quarantined-pending-gvisor",)
                try:
                    quarantine = await self._discovery.fetch_into_quarantine(
                        discovered,
                        budget=DiscoveryBudget(
                            max_providers=1,
                            max_results=1,
                            max_bytes=16 * 1024 * 1024,
                            max_runtime_ms=30_000,
                        ),
                    )
                    if discovered.expected_digest and quarantine.digest != discovered.expected_digest:
                        raise RemoteMcpError("published package digest does not match fetched bytes")
                    spec = {
                        "serverId": discovered.name,
                        "acquisitionTaskId": task_id,
                        "package": {
                            "source": discovered.source_uri,
                            "sha256": quarantine.digest,
                            "sizeBytes": quarantine.size_bytes,
                            "executable": False,
                        },
                        "qualification": {"state": "quarantined", "reason": reasons[0]},
                    }
                    artifact = create_artifact(
                        artifact_id=f"mcp.adapter.{hashlib.sha256(discovered.identity.encode()).hexdigest()[:24]}",
                        version=discovered.version or "unversioned",
                        kind=ArtifactKind.MCP_ADAPTER,
                        owner=ArtifactOwner.EXTERNAL,
                        origin=ArtifactOrigin.MCP,
                        spec=spec,
                        created_at=self._created_at(int(self._clock_ms())),
                        user_id=user_id,
                        task_origin=task_id,
                        source_digest=digest_payload(discovered.to_dict()),
                        state=ArtifactState.EPHEMERAL,
                    )
                    row = await self._store.persist_artifact(artifact)
                    digest = row.content_digest
                    state = row.state
                except (DiscoveryBudgetExceeded, RemoteMcpError, ValueError):
                    digest = ""
                    state = "rejected"
                    reasons = ("package-quarantine-failed",)
                results.append(
                    AcquiredMcpAdapter(
                        artifact_digest=digest,
                        server_id=discovered.name,
                        version=discovered.version or "unversioned",
                        state=state,
                        eligible=False,
                        reasons=reasons,
                        projected_match=(),
                    )
                )
        return results

    async def require_mount_candidate(self, row, *, goal: AcquisitionGoal) -> tuple[McpCandidate, str]:
        if row.kind != ArtifactKind.MCP_ADAPTER.value:
            raise ValueError("MCP mount requires an McpAdapter artifact")
        if row.task_origin != goal.task_id:
            raise PermissionError("MCP adapter belongs to a different acquisition task")
        if row.state != ArtifactState.QUALIFIED.value:
            raise PermissionError("MCP adapter is not qualified for remote mounting")
        spec = dict(row.spec or {})
        remote = spec.get("remote") if isinstance(spec.get("remote"), dict) else {}
        remote_url = str(remote.get("url") or "").strip()
        if remote.get("transport") != "streamable-http" or not remote_url:
            raise PermissionError("MCP adapter is not a remote Streamable HTTP adapter")
        observation = await self._connector.probe(remote_url)
        expected = str(spec.get("identityFingerprint") or "")
        if not expected or observation.fingerprint != expected:
            raise PermissionError("remote MCP identity changed; reacquisition is required")
        candidate = self._candidate_from_artifact(row)
        qualification = self._fabric.qualify(candidate)
        if not qualification.eligible:
            raise PermissionError("MCP adapter failed server qualification")
        available = set(candidate.tools)
        for tool in goal.required:
            if tool not in available:
                raise PermissionError("MCP adapter no longer exposes a required projected tool")
        return candidate, observation.remote_url


class ProjectedMcpActionExecutor:
    """Route only ``mcp://<mount-id>/<tool>`` actions through active projections."""

    def __init__(
        self,
        *,
        base_executor: Any,
        store: SqlCapabilityOsStore,
        authority: AuthorityBroker,
        connector: StreamableHttpMcpConnector,
    ) -> None:
        self._base = base_executor
        self._store = store
        self._authority = authority
        self._connector = connector

    @staticmethod
    def _parse(action_ref: str) -> tuple[str, str] | None:
        if not str(action_ref).startswith("mcp://"):
            return None
        rest = str(action_ref)[6:]
        mount_id, sep, tool = rest.partition("/")
        if not sep or not mount_id.strip() or not tool.strip() or "/" in mount_id:
            raise PermissionError("invalid projected MCP action reference")
        return mount_id.strip(), tool.strip()

    async def invoke(
        self,
        *,
        action_ref: str,
        version: str,
        inputs: dict[str, Any],
        lease: CapabilityLease,
        timeout_ms: int,
    ) -> ActionResult:
        parsed = self._parse(action_ref)
        if parsed is None:
            return await self._base.invoke(
                action_ref=action_ref,
                version=version,
                inputs=inputs,
                lease=lease,
                timeout_ms=timeout_ms,
            )
        mount_id, tool = parsed
        mount = await self._store.get_mcp_mount(mount_id)
        if mount is None or mount.state != "mounted":
            raise PermissionError("projected MCP mount is not active")
        if mount.task_id != lease.task_id:
            raise PermissionError("projected MCP mount belongs to another task")
        if tool not in tuple(mount.projected_tools or []):
            raise PermissionError("MCP tool is outside the mounted projection")
        artifact = await self._store.get_artifact(mount.digest)
        if artifact is None or artifact.kind != ArtifactKind.MCP_ADAPTER.value:
            raise PermissionError("projected MCP adapter artifact is unavailable")
        spec = dict(artifact.spec or {})
        remote = spec.get("remote") if isinstance(spec.get("remote"), dict) else {}
        remote_url = str(remote.get("url") or "").strip()
        server_id = str(spec.get("serverId") or "").strip()
        if not remote_url or not server_id:
            raise PermissionError("projected MCP adapter identity is incomplete")
        if str(version) != str(mount.version):
            raise PermissionError("projected MCP action version does not match the mounted adapter")
        required = CapabilityRequest("mcp.invoke", f"mcp:{server_id}/{tool}")
        await self._authority.require_active(
            lease.lease_id,
            task_id=lease.task_id,
            artifact_digest=lease.artifact_digest,
            required_permissions=(required,),
            runtime_profile="cptr-vm",
        )
        await self._authority.require_active(
            mount.lease_id,
            task_id=mount.task_id,
            artifact_digest=mount.digest,
            required_permissions=(required,),
            runtime_profile="remote-mcp",
            network_destinations=(remote_url,),
        )
        return await asyncio.wait_for(
            self._connector.invoke(remote_url=remote_url, tool_name=tool, arguments=dict(inputs)),
            timeout=max(0.001, int(timeout_ms) / 1000),
        )
