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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from cptr.services.capability_os.authority import AuthorityBroker, CapabilityLease
from cptr.services.capability_os.credential_broker import CredentialBroker
from cptr.services.capability_os.evidence import EvidenceService
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
from cptr.services.capability_os.mcp_discovery_index import McpSemanticDiscoveryIndex
from cptr.services.capability_os.mcp_fabric import AcquisitionGoal, McpCandidate, McpFabric
from cptr.services.capability_os.mcp_reputation import McpReputationService
from cptr.services.capability_os.mcp_package import (
    BRIDGE_ENTRYPOINT,
    McpbPackageError,
    McpbPackagePreparer,
)
from cptr.services.capability_os.runtime import RuntimeClass
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


async def _resolve_public_tcp_addresses(host: str, port: int) -> tuple[str, ...]:
    normalized = str(host or "").strip().lower().rstrip(".")
    if not normalized:
        raise RemoteMcpError("remote MCP endpoint host must not be blank")
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None:
        value = str(literal)
        if not _is_public_ip(value):
            raise RemoteMcpError("remote MCP endpoint must target a public address")
        return (value,)
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            normalized,
            int(port),
            0,
            socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise RemoteMcpError("remote MCP endpoint could not be resolved") from exc
    resolved = tuple(dict.fromkeys(str(entry[4][0]) for entry in addresses if entry and entry[4]))
    if not resolved or any(not _is_public_ip(address) for address in resolved):
        raise RemoteMcpError("remote MCP endpoint DNS resolved to a non-public address")
    return resolved


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


@dataclass(frozen=True)
class RemoteMcpAuthBinding:
    """Server-owned binding from one exact remote MCP identity to one logical credential."""

    server_id: str
    remote_url: str
    logical_name: str
    mechanism: str = "bearer"

    def __post_init__(self) -> None:
        server_id = str(self.server_id).strip()
        remote_url = str(self.remote_url).strip()
        logical_name = str(self.logical_name).strip()
        mechanism = str(self.mechanism).strip().lower()
        if not server_id or not remote_url or not logical_name:
            raise ValueError("remote MCP auth binding fields must not be blank")
        if mechanism != "bearer":
            raise ValueError("remote MCP auth binding mechanism must be bearer")
        if len(server_id) > 512 or len(remote_url) > 4096 or len(logical_name) > 512:
            raise ValueError("remote MCP auth binding exceeds size limits")
        object.__setattr__(self, "server_id", server_id)
        object.__setattr__(self, "remote_url", remote_url)
        object.__setattr__(self, "logical_name", logical_name)
        object.__setattr__(self, "mechanism", mechanism)

    @property
    def consumer(self) -> str:
        return f"mcp.remote:{self.remote_url}"


class ConfigRemoteMcpAuthProvider:
    """Resolve exact remote MCP credential bindings from operator-owned Config.

    Discovery metadata and public API payloads cannot create these bindings. A
    malformed or ambiguous configured match fails closed.
    """

    def __init__(
        self,
        *,
        config_getter: Callable[[str], Awaitable[Any]] | None = None,
        config_key: str = "capability_os.mcp_remote_auth_bindings",
    ) -> None:
        self._config_getter = config_getter
        self._config_key = str(config_key).strip()
        if not self._config_key:
            raise ValueError("remote MCP auth config key must not be blank")

    async def _get(self) -> Any:
        if self._config_getter is not None:
            return await self._config_getter(self._config_key)
        from cptr.models import Config

        return await Config.get(self._config_key)

    async def resolve(self, *, server_id: str, remote_url: str) -> RemoteMcpAuthBinding | None:
        raw = await self._get()
        if raw is None:
            return None
        if not isinstance(raw, list) or len(raw) > 256:
            raise RemoteMcpError("remote MCP auth configuration is invalid")
        matches: list[RemoteMcpAuthBinding] = []
        try:
            for item in raw:
                if not isinstance(item, dict) or item.get("enabled", True) is not True:
                    continue
                unknown = set(item) - {
                    "enabled",
                    "serverId",
                    "remoteUrl",
                    "logicalName",
                    "mechanism",
                }
                if unknown:
                    raise ValueError("remote MCP auth binding contains unknown fields")
                binding = RemoteMcpAuthBinding(
                    server_id=str(item.get("serverId") or ""),
                    remote_url=str(item.get("remoteUrl") or ""),
                    logical_name=str(item.get("logicalName") or ""),
                    mechanism=str(item.get("mechanism") or "bearer"),
                )
                if (
                    binding.server_id == str(server_id).strip()
                    and binding.remote_url == str(remote_url).strip()
                ):
                    matches.append(binding)
        except (TypeError, ValueError) as exc:
            raise RemoteMcpError("remote MCP auth configuration is invalid") from exc
        if len(matches) > 1:
            raise RemoteMcpError("remote MCP auth configuration is ambiguous")
        return matches[0] if matches else None


@dataclass(frozen=True)
class AuthenticatedRemoteMcpQualification:
    artifact_digest: str
    candidate: McpCandidate
    observation: RemoteMcpObservation
    projected_match: tuple[str, ...]


@dataclass(frozen=True)
class PackagedMcpQualification:
    artifact_digest: str
    candidate: McpCandidate
    protocol_version: str
    server_name: str
    server_version: str
    attestation: dict[str, Any]
    projected_match: tuple[str, ...]


class PublicHttpsEndpointValidator:
    """Reject local/private MCP endpoints and normalize public HTTPS URLs."""

    @staticmethod
    def _is_public_ip(value: str) -> bool:
        return _is_public_ip(value)

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
        await _resolve_public_tcp_addresses(host, parsed.port or 443)
        netloc = host
        if ":" in host and not host.startswith("["):
            netloc = f"[{host}]"
        if parsed.port is not None and parsed.port != 443:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


class _PublicOnlyNetworkBackend:
    """Resolve and dial only public IP literals at the actual socket boundary.

    HTTP/TLS still retains the original hostname, so certificate/SNI validation
    remains hostname-bound while DNS rebinding cannot redirect the socket dial.
    """

    def __init__(self, *, delegate) -> None:
        self._delegate = delegate

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        addresses = await _resolve_public_tcp_addresses(host, port)
        deadline = time.monotonic() + timeout if timeout is not None else None
        last_error: Exception | None = None
        for address in addresses:
            remaining = None if deadline is None else max(0.001, deadline - time.monotonic())
            try:
                return await self._delegate.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_error = exc
                if deadline is not None and time.monotonic() >= deadline:
                    break
        if last_error is not None:
            raise last_error
        raise RemoteMcpError("remote MCP endpoint has no dialable public address")

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options=None
    ):
        del path, timeout, socket_options
        raise RemoteMcpError("remote MCP transport does not permit Unix sockets")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


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
        except (
            Exception
        ) as exc:  # pragma: no cover - environment-specific missing optional dependency
            raise RemoteMcpError("official MCP SDK is not installed") from exc
        return ClientSession, streamable_http_client

    @staticmethod
    def credential_consumer(remote_url: str) -> str:
        return f"mcp.remote:{str(remote_url).strip()}"

    async def validate_url(self, remote_url: str) -> str:
        return await self._validator.validate(remote_url)

    @staticmethod
    def _bearer_value(secret: str | bytes) -> str:
        if isinstance(secret, bytes):
            try:
                value = secret.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RemoteMcpError("remote MCP credential is not UTF-8") from exc
        else:
            value = str(secret)
        value = value.strip()
        if (
            not value
            or len(value) > 16_384
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise RemoteMcpError("remote MCP credential is invalid")
        return value

    def _http_client(self, *, bearer_token: str | None = None) -> httpx.AsyncClient:
        headers = {
            "User-Agent": "cptr-capability-os",
            "Accept": "application/json, text/event-stream",
        }
        if bearer_token is not None:
            headers["Authorization"] = f"Bearer {bearer_token}"
        transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
        pool = getattr(transport, "_pool", None)
        network_backend = getattr(pool, "_network_backend", None)
        if pool is None or network_backend is None:
            raise RemoteMcpError(
                "remote MCP HTTP transport cannot enforce public-only socket dialing"
            )
        pool._network_backend = _PublicOnlyNetworkBackend(delegate=network_backend)
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers=headers,
            transport=transport,
        )

    async def _with_http_client(
        self,
        *,
        url: str,
        credential_broker: CredentialBroker | None,
        credential_lease: CapabilityLease | None,
        credential_name: str | None,
        operation,
    ):
        name = str(credential_name or "").strip()
        if not name:
            async with self._http_client() as http_client:
                return await operation(http_client)
        if credential_broker is None or credential_lease is None:
            raise RemoteMcpError("remote MCP credential authority is unavailable")
        consumer = self.credential_consumer(url)

        async def consume(secret: str | bytes):
            token = self._bearer_value(secret)
            try:
                async with self._http_client(bearer_token=token) as http_client:
                    return await operation(http_client)
            finally:
                token = ""

        return await credential_broker.inject(
            lease=credential_lease,
            logical_name=name,
            consumer=consumer,
            operation=consume,
            ttl_ms=max(1_000, min(30_000, int(self._timeout_seconds * 1000) + 1_000)),
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

    async def _probe_with_client(
        self, url: str, http_client: httpx.AsyncClient
    ) -> RemoteMcpObservation:
        ClientSession, streamable_http_client = self._sdk()
        tools: list[RemoteMcpTool] = []
        seen_names: set[str] = set()
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
                            raise RemoteMcpError(
                                "remote MCP tool pagination exceeds the page bound"
                            )
        except RemoteMcpError:
            raise
        except Exception as exc:
            raise RemoteMcpError(f"remote MCP handshake failed: {exc.__class__.__name__}") from exc
        info = getattr(initialized, "serverInfo", None)
        return RemoteMcpObservation(
            remote_url=url,
            protocol_version=str(getattr(initialized, "protocolVersion", "") or "unknown")[:128],
            server_name=str(getattr(info, "name", "") or "unknown")[:256],
            server_version=str(getattr(info, "version", "") or "unknown")[:128],
            tools=tuple(tools),
        )

    async def probe(
        self,
        remote_url: str,
        *,
        credential_broker: CredentialBroker | None = None,
        credential_lease: CapabilityLease | None = None,
        credential_name: str | None = None,
    ) -> RemoteMcpObservation:
        url = await self.validate_url(remote_url)
        return await self._with_http_client(
            url=url,
            credential_broker=credential_broker,
            credential_lease=credential_lease,
            credential_name=credential_name,
            operation=lambda http_client: self._probe_with_client(url, http_client),
        )

    async def _invoke_with_client(
        self,
        *,
        url: str,
        tool_name: str,
        arguments: dict[str, Any],
        http_client: httpx.AsyncClient,
    ) -> ActionResult:
        ClientSession, streamable_http_client = self._sdk()
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
        else:
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

    async def invoke(
        self,
        *,
        remote_url: str,
        tool_name: str,
        arguments: dict[str, Any],
        credential_broker: CredentialBroker | None = None,
        credential_lease: CapabilityLease | None = None,
        credential_name: str | None = None,
    ) -> ActionResult:
        url = await self.validate_url(remote_url)
        if not isinstance(arguments, dict):
            raise RemoteMcpError("remote MCP tool arguments must be an object")
        return await self._with_http_client(
            url=url,
            credential_broker=credential_broker,
            credential_lease=credential_lease,
            credential_name=credential_name,
            operation=lambda http_client: self._invoke_with_client(
                url=url, tool_name=tool_name, arguments=arguments, http_client=http_client
            ),
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
        package_preparer: McpbPackagePreparer | None = None,
        package_runner: Any = None,
        package_resources: dict[str, Any] | None = None,
        auth_provider: ConfigRemoteMcpAuthProvider | None = None,
        oauth_profile_provider: Any = None,
        credential_broker: CredentialBroker | None = None,
        reputation: McpReputationService | None = None,
        semantic_index: McpSemanticDiscoveryIndex | None = None,
        clock_ms=lambda: int(time.time() * 1000),
        max_candidates: int = 8,
    ) -> None:
        self._store = store
        self._fabric = fabric
        self._discovery = discovery
        self._connector = connector
        self._auth_provider = auth_provider
        self._oauth_profile_provider = oauth_profile_provider
        self._credential_broker = credential_broker
        self._reputation = reputation or McpReputationService(store=store)
        self._semantic_index = semantic_index
        self._package_preparer = package_preparer
        self._package_runner = package_runner
        self._package_resources = dict(package_resources or {})
        self._clock_ms = clock_ms
        self._max_candidates = max(1, min(int(max_candidates), 32))

    @staticmethod
    def _created_at(now_ms: int) -> str:
        return (
            datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _remote_entries(candidate: DiscoveryCandidate) -> tuple[str, ...]:
        remotes = (
            candidate.metadata.get("remotes") if isinstance(candidate.metadata, dict) else None
        )
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
    def _package_entries(candidate: DiscoveryCandidate) -> tuple[dict[str, Any], ...]:
        packages = (
            candidate.metadata.get("packages") if isinstance(candidate.metadata, dict) else None
        )
        if not isinstance(packages, list):
            return ()
        return tuple(item for item in packages[:20] if isinstance(item, dict) and item)

    @staticmethod
    def _package_kind(spec: dict[str, Any]) -> str:
        package = spec.get("package") if isinstance(spec.get("package"), dict) else {}
        return str(package.get("registryType") or "").strip().lower()

    @staticmethod
    def is_packaged_adapter(row) -> bool:
        spec = dict(row.spec or {})
        return isinstance(spec.get("package"), dict)

    @staticmethod
    def _remote_auth(spec: dict[str, Any]) -> dict[str, Any]:
        auth = spec.get("authentication")
        return dict(auth) if isinstance(auth, dict) else {}

    @classmethod
    def is_authenticated_remote(cls, row) -> bool:
        spec = dict(row.spec or {})
        remote = spec.get("remote") if isinstance(spec.get("remote"), dict) else {}
        auth = cls._remote_auth(spec)
        return (
            remote.get("transport") == "streamable-http"
            and bool(str(remote.get("url") or "").strip())
            and auth.get("mechanism") == "bearer"
            and bool(str(auth.get("logicalName") or "").strip())
        )

    @classmethod
    def remote_auth_details(cls, row) -> tuple[str, str, str]:
        if not cls.is_authenticated_remote(row):
            raise PermissionError("MCP adapter does not have server-owned remote authentication")
        spec = dict(row.spec or {})
        remote = dict(spec.get("remote") or {})
        auth = cls._remote_auth(spec)
        remote_url = str(remote.get("url") or "").strip()
        logical_name = str(auth.get("logicalName") or "").strip()
        consumer = str(auth.get("consumer") or "").strip()
        expected_consumer = StreamableHttpMcpConnector.credential_consumer(remote_url)
        if consumer != expected_consumer:
            raise PermissionError("remote MCP credential consumer binding is invalid")
        return remote_url, logical_name, consumer

    @staticmethod
    def _candidate_from_artifact(row) -> McpCandidate:
        spec = dict(row.spec or {})
        remote = spec.get("remote") if isinstance(spec.get("remote"), dict) else {}
        package = spec.get("package") if isinstance(spec.get("package"), dict) else {}
        tool_rows = spec.get("tools") if isinstance(spec.get("tools"), list) else []
        tools = tuple(
            str(item.get("name") or "")
            for item in tool_rows
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        )
        server_id = str(spec.get("serverId") or "").strip()
        permission_rows = (
            spec.get("permissions") if isinstance(spec.get("permissions"), list) else []
        )
        permissions = tuple(
            CapabilityRequest.from_dict(item) for item in permission_rows if isinstance(item, dict)
        )
        return McpCandidate(
            server_id=server_id,
            version=str(row.version),
            digest=str(row.content_digest),
            transport_kind=(
                str(remote.get("transport") or "")
                if remote
                else str(package.get("transport") or "")
            ),
            tools=tools,
            permissions=permissions,
            identity_ok=True,
            auth_ok=True,
            sandboxable=True,
            hard_denies=(),
        )

    async def _candidate_with_reputation(self, row) -> McpCandidate:
        candidate = self._candidate_from_artifact(row)
        if not row.user_id:
            return candidate
        reputation = await self._reputation.snapshot(
            user_id=row.user_id,
            server_id=candidate.server_id,
        )
        return replace(
            candidate,
            successes=reputation.successes,
            failures=reputation.failures,
        )

    async def reputation_snapshot(self, *, user_id: str, server_id: str):
        return await self._reputation.snapshot(user_id=user_id, server_id=server_id)

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
        if self._semantic_index is not None:
            await self._semantic_index.sync_candidates(batch.candidates)
        results: list[AcquiredMcpAdapter] = []
        for discovered in batch.candidates[: self._max_candidates]:
            if discovered.provider != "mcp_registry" or discovered.candidate_type != "mcp_server":
                continue
            remotes = self._remote_entries(discovered)
            if remotes:
                for remote_url in remotes:
                    try:
                        validator = getattr(self._connector, "validate_url", None)
                        if callable(validator):
                            normalized_url = validator(remote_url)
                            if asyncio.iscoroutine(normalized_url):
                                normalized_url = await normalized_url
                            normalized_url = str(normalized_url)
                        else:
                            normalized_url = str(remote_url).strip()
                        auth_binding = (
                            await self._auth_provider.resolve(
                                server_id=discovered.name,
                                remote_url=normalized_url,
                            )
                            if self._auth_provider is not None
                            else None
                        )
                        oauth_profile = (
                            await self._oauth_profile_provider.find(
                                server_id=discovered.name,
                                remote_url=normalized_url,
                            )
                            if self._oauth_profile_provider is not None
                            else None
                        )
                        if auth_binding is not None and oauth_profile is not None:
                            raise RemoteMcpError(
                                "remote MCP has ambiguous bearer and OAuth authentication"
                            )
                    except Exception as exc:
                        results.append(
                            AcquiredMcpAdapter(
                                artifact_digest="",
                                server_id=discovered.name,
                                version=discovered.version or "unversioned",
                                state="rejected",
                                eligible=False,
                                reasons=(f"remote-validation:{exc.__class__.__name__}",),
                                projected_match=(),
                            )
                        )
                        continue
                    if auth_binding is not None:
                        permission = self._permission(discovered.name)
                        spec = {
                            "serverId": discovered.name,
                            "acquisitionTaskId": task_id,
                            "registryStableId": discovered.stable_id,
                            "registryIdentity": discovered.identity,
                            "remote": {"url": normalized_url, "transport": "streamable-http"},
                            "authentication": {
                                "mechanism": auth_binding.mechanism,
                                "logicalName": auth_binding.logical_name,
                                "consumer": auth_binding.consumer,
                            },
                            "permissions": [permission.to_dict()],
                            "qualification": {
                                "state": "credential-required",
                                "identity": "registry+endpoint-validation",
                                "auth": "server-owned-logical-credential",
                                "sandbox": "remote-no-local-execution",
                            },
                        }
                        artifact = create_artifact(
                            artifact_id=f"mcp.adapter.{hashlib.sha256((discovered.name + normalized_url).encode()).hexdigest()[:24]}",
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
                        results.append(
                            AcquiredMcpAdapter(
                                artifact_digest=row.content_digest,
                                server_id=discovered.name,
                                version=row.version,
                                state=row.state,
                                eligible=False,
                                reasons=("remote-authentication-required",),
                                projected_match=(),
                            )
                        )
                        continue
                    if oauth_profile is not None:
                        permission = self._permission(discovered.name)
                        spec = {
                            "serverId": discovered.name,
                            "acquisitionTaskId": task_id,
                            "registryStableId": discovered.stable_id,
                            "registryIdentity": discovered.identity,
                            "remote": {"url": normalized_url, "transport": "streamable-http"},
                            "authentication": {
                                "mechanism": "oauth2",
                                "logicalName": str(oauth_profile.profile_id),
                                "source": "operator-profile",
                            },
                            "permissions": [permission.to_dict()],
                            "qualification": {
                                "state": "oauth-required",
                                "identity": "registry+endpoint-validation",
                                "auth": "server-owned-oauth2",
                                "sandbox": "remote-no-local-execution",
                            },
                        }
                        artifact = create_artifact(
                            artifact_id=f"mcp.adapter.{hashlib.sha256((discovered.name + normalized_url).encode()).hexdigest()[:24]}",
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
                        results.append(
                            AcquiredMcpAdapter(
                                artifact_digest=row.content_digest,
                                server_id=discovered.name,
                                version=row.version,
                                state=row.state,
                                eligible=False,
                                reasons=("remote-oauth-authorization-required",),
                                projected_match=(),
                            )
                        )
                        continue
                    try:
                        observation = await self._connector.probe(normalized_url)
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
                        [
                            *(f"missing-tool:{item}" for item in missing),
                            *(f"forbidden-tool:{item}" for item in required_forbidden),
                        ]
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
                            projected_match=tuple(
                                item for item in required if item in set(tool_names)
                            ),
                        )
                    )
                continue

            packages = self._package_entries(discovered)
            if packages:
                package = next(
                    (
                        item
                        for item in packages
                        if str(item.get("registryType") or "").strip().lower() == "mcpb"
                    ),
                    packages[0],
                )
                registry_type = str(package.get("registryType") or "").strip().lower()
                digest = ""
                state = "rejected"
                reasons: tuple[str, ...]
                try:
                    if registry_type != "mcpb":
                        reasons = ("package-install-runtime-disabled",)
                        spec = {
                            "serverId": discovered.name,
                            "acquisitionTaskId": task_id,
                            "registryStableId": discovered.stable_id,
                            "registryIdentity": discovered.identity,
                            "package": {
                                **package,
                                "source": None,
                                "executable": False,
                            },
                            "qualification": {"state": "quarantined", "reason": reasons[0]},
                        }
                        artifact = create_artifact(
                            artifact_id=(
                                "mcp.adapter."
                                + hashlib.sha256(discovered.identity.encode()).hexdigest()[:24]
                            ),
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
                        digest, state = row.content_digest, row.state
                    else:
                        if not discovered.source_uri or not discovered.expected_digest:
                            raise McpbPackageError(
                                "MCPB package requires an HTTPS artifact URL and published SHA-256"
                            )
                        quarantine = await self._discovery.fetch_into_quarantine(
                            discovered,
                            budget=DiscoveryBudget(
                                max_providers=1,
                                max_results=1,
                                max_bytes=16 * 1024 * 1024,
                                max_runtime_ms=30_000,
                            ),
                        )
                        if quarantine.digest != discovered.expected_digest:
                            raise RemoteMcpError(
                                "published package digest does not match fetched bytes"
                            )
                        if self._package_preparer is None or self._package_runner is None:
                            raise McpbPackageError("MCPB gVisor runtime is not configured")
                        content = quarantine.read_bytes(max_bytes=16 * 1024 * 1024)
                        prepared = self._package_preparer.prepare(
                            content, expected_version=discovered.version
                        )
                        reasons = ("package-awaiting-gvisor-qualification",)
                        permission = self._permission(discovered.name)
                        spec = {
                            "serverId": discovered.name,
                            "acquisitionTaskId": task_id,
                            "registryStableId": discovered.stable_id,
                            "registryIdentity": discovered.identity,
                            "package": {
                                **package,
                                "source": discovered.source_uri,
                                "sha256": quarantine.digest,
                                "sizeBytes": quarantine.size_bytes,
                                "transport": "stdio-gvisor",
                                "bundleDigest": prepared.bundle_digest,
                                "manifestDigest": prepared.manifest_digest,
                                "manifestVersion": prepared.manifest_version,
                                "packageName": prepared.package_name,
                                "serverEntrypoint": prepared.server_entrypoint,
                                "fileCount": prepared.file_count,
                                "executable": False,
                            },
                            "runtime": {
                                "class": "gvisor",
                                "language": "python",
                                "entrypoint": BRIDGE_ENTRYPOINT,
                            },
                            "resources": dict(self._package_resources),
                            "permissions": [permission.to_dict()],
                            "qualification": {"state": "quarantined", "reason": reasons[0]},
                        }
                        artifact = create_artifact(
                            artifact_id=(
                                "mcp.adapter."
                                + hashlib.sha256(discovered.identity.encode()).hexdigest()[:24]
                            ),
                            version=discovered.version or prepared.package_version,
                            kind=ArtifactKind.MCP_ADAPTER,
                            owner=ArtifactOwner.EXTERNAL,
                            origin=ArtifactOrigin.MCP,
                            spec=spec,
                            created_at=self._created_at(int(self._clock_ms())),
                            user_id=user_id,
                            task_origin=task_id,
                            source_digest=prepared.bundle_digest,
                            state=ArtifactState.EPHEMERAL,
                        )
                        row = await self._store.persist_artifact(artifact)
                        digest, state = row.content_digest, row.state
                except (DiscoveryBudgetExceeded, RemoteMcpError, McpbPackageError, ValueError):
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

    async def discover_by_effects(
        self,
        *,
        required_effects: tuple[str, ...],
        forbidden_effects: tuple[str, ...] = (),
        refresh_query: str | None = None,
        limit: int = 20,
    ) -> tuple[dict[str, Any], ...]:
        if self._semantic_index is None:
            raise RemoteMcpError("semantic MCP discovery index is not configured")
        query = str(refresh_query or "").strip()
        if query:
            batch = await self._discovery.discover(
                query,
                signals=ResearchSignals(unfamiliar_technology=True, api_uncertain=True),
                budget=DiscoveryBudget(
                    max_providers=8,
                    max_results=max(1, min(int(limit) * 4, 100)),
                    max_bytes=1_000_000,
                    max_runtime_ms=20_000,
                ),
            )
            await self._semantic_index.sync_candidates(batch.candidates)
        qualified = await self._store.list_artifacts(
            kinds=(ArtifactKind.MCP_ADAPTER.value,),
            states=(
                ArtifactState.QUALIFIED.value,
                ArtifactState.LEARNED.value,
                ArtifactState.CERTIFIED.value,
            ),
            limit=1000,
        )
        for row in qualified:
            await self._semantic_index.sync_qualified_adapter(row)
        matches = await self._semantic_index.query(
            required_effects=required_effects,
            forbidden_effects=forbidden_effects,
            limit=limit,
        )
        return tuple(item.to_api() for item in matches)

    @staticmethod
    def _packaged_live_tools(output: Any) -> tuple[RemoteMcpTool, ...]:
        if not isinstance(output, dict) or output.get("ok") is not True:
            raise RemoteMcpError("packaged MCP live probe failed")
        raw_tools = output.get("tools")
        if not isinstance(raw_tools, list) or len(raw_tools) > 128:
            raise RemoteMcpError("packaged MCP returned an invalid tool list")
        tools: list[RemoteMcpTool] = []
        seen: set[str] = set()
        for item in raw_tools:
            if not isinstance(item, dict):
                raise RemoteMcpError("packaged MCP returned an invalid tool descriptor")
            name = str(item.get("name") or "").strip()
            schema = item.get("inputSchema")
            if not name or len(name) > 256 or name in seen or not isinstance(schema, dict):
                raise RemoteMcpError("packaged MCP returned an invalid tool descriptor")
            seen.add(name)
            tools.append(RemoteMcpTool(name=name, input_schema_digest=digest_payload(schema)))
        return tuple(tools)

    async def qualify_authenticated_remote(
        self,
        row,
        *,
        goal: AcquisitionGoal,
        lease: CapabilityLease,
    ) -> AuthenticatedRemoteMcpQualification:
        if self._credential_broker is None:
            raise RemoteMcpError("remote MCP credential broker is unavailable")
        if row.kind != ArtifactKind.MCP_ADAPTER.value or row.task_origin != goal.task_id:
            raise PermissionError("remote MCP adapter belongs to another task")
        if row.state != ArtifactState.EPHEMERAL.value:
            raise PermissionError(
                "authenticated remote MCP qualification requires an ephemeral adapter"
            )
        remote_url, logical_name, _consumer = self.remote_auth_details(row)
        observation = await self._connector.probe(
            remote_url,
            credential_broker=self._credential_broker,
            credential_lease=lease,
            credential_name=logical_name,
        )
        available = {tool.name for tool in observation.tools}
        required = tuple(dict.fromkeys(item.strip() for item in goal.required if item.strip()))
        missing = [item for item in required if item not in available]
        forbidden = {item.strip() for item in goal.forbidden if item.strip()}
        if missing:
            raise PermissionError(f"remote MCP is missing required tool: {missing[0]}")
        if any(item in forbidden for item in required):
            raise PermissionError("remote MCP goal requires a forbidden tool")
        spec = dict(row.spec or {})
        qualified_spec = dict(spec)
        qualified_spec["protocolVersion"] = observation.protocol_version
        qualified_spec["serverInfo"] = {
            "name": observation.server_name,
            "version": observation.server_version,
        }
        qualified_spec["identityFingerprint"] = observation.fingerprint
        qualified_spec["tools"] = [
            {"name": tool.name, "inputSchemaDigest": tool.input_schema_digest}
            for tool in observation.tools
        ]
        qualified_spec["qualification"] = {
            "state": "credential-qualified",
            "identity": "registry+credentialed-live-handshake",
            "auth": "credential-brokered-bearer",
            "sandbox": "remote-no-local-execution",
        }
        parent = f"{row.artifact_id}@{row.version}#{row.content_digest}"
        artifact = create_artifact(
            artifact_id=row.artifact_id,
            version=row.version,
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec=qualified_spec,
            created_at=self._created_at(int(self._clock_ms())),
            user_id=row.user_id,
            parent=parent,
            task_origin=row.task_origin,
            source_digest=row.source_digest,
            state=ArtifactState.EPHEMERAL,
        )
        qualified_row = await self._store.persist_artifact(artifact)
        return AuthenticatedRemoteMcpQualification(
            artifact_digest=qualified_row.content_digest,
            candidate=await self._candidate_with_reputation(qualified_row),
            observation=observation,
            projected_match=tuple(item for item in required if item in available),
        )

    async def qualify_packaged(
        self,
        row,
        *,
        goal: AcquisitionGoal,
        lease: CapabilityLease,
        timeout_ms: int | None = None,
    ) -> PackagedMcpQualification:
        if self._package_runner is None:
            raise RemoteMcpError("packaged MCP gVisor runner is unavailable")
        if row.kind != ArtifactKind.MCP_ADAPTER.value or row.task_origin != goal.task_id:
            raise PermissionError("packaged MCP adapter belongs to another task")
        if row.state != ArtifactState.EPHEMERAL.value:
            raise PermissionError("packaged MCP qualification requires a quarantined adapter")
        spec = dict(row.spec or {})
        package = spec.get("package") if isinstance(spec.get("package"), dict) else {}
        if self._package_kind(spec) != "mcpb" or package.get("transport") != "stdio-gvisor":
            raise PermissionError("MCP adapter is not an enabled MCPB package")
        if not row.source_digest or str(package.get("bundleDigest") or "") != row.source_digest:
            raise PermissionError("packaged MCP immutable bundle identity is incomplete")
        resources = dict(spec.get("resources") or {})
        wall_time_ms = int(resources.get("wallTimeMs") or 0)
        bounded_timeout = int(timeout_ms or wall_time_ms)
        if bounded_timeout <= 0 or wall_time_ms <= 0:
            raise RemoteMcpError("packaged MCP runtime bounds are unavailable")
        result = self._package_runner(
            runtime_class=RuntimeClass.GVISOR,
            artifact=row,
            lease=lease,
            inputs={"mode": "probe", "protocolVersion": "2025-06-18"},
            timeout_ms=min(bounded_timeout, wall_time_ms),
        )
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, ActionResult):
            raise TypeError("packaged MCP runner returned an invalid result")
        output = result.output
        tools = self._packaged_live_tools(output)
        available = {tool.name for tool in tools}
        required = tuple(dict.fromkeys(item.strip() for item in goal.required if item.strip()))
        missing = [item for item in required if item not in available]
        forbidden = {item.strip() for item in goal.forbidden if item.strip()}
        if missing:
            raise PermissionError(f"packaged MCP is missing required tool: {missing[0]}")
        if any(item in forbidden for item in required):
            raise PermissionError("packaged MCP goal requires a forbidden tool")
        server_info = output.get("serverInfo") if isinstance(output, dict) else {}
        if not isinstance(server_info, dict):
            server_info = {}
        protocol_version = str(output.get("protocolVersion") or "unknown")[:128]
        server_name = str(server_info.get("name") or package.get("packageName") or "unknown")[:256]
        server_version = str(server_info.get("version") or row.version or "unknown")[:128]
        fingerprint = digest_payload(
            {
                "packageDigest": package.get("sha256"),
                "bundleDigest": row.source_digest,
                "manifestDigest": package.get("manifestDigest"),
                "protocolVersion": protocol_version,
                "serverName": server_name,
                "serverVersion": server_version,
                "tools": [
                    {"name": tool.name, "inputSchemaDigest": tool.input_schema_digest}
                    for tool in tools
                ],
            }
        )
        qualified_spec = dict(spec)
        qualified_spec["package"] = {**package, "executable": True}
        qualified_spec["protocolVersion"] = protocol_version
        qualified_spec["serverInfo"] = {"name": server_name, "version": server_version}
        qualified_spec["identityFingerprint"] = fingerprint
        qualified_spec["tools"] = [
            {"name": tool.name, "inputSchemaDigest": tool.input_schema_digest} for tool in tools
        ]
        qualified_spec["qualification"] = {
            "state": "sandbox-qualified",
            "identity": "registry-digest+mcpb-manifest+live-stdio-handshake",
            "auth": "no-ambient-credentials",
            "sandbox": "gvisor-network-deny",
        }
        parent = f"{row.artifact_id}@{row.version}#{row.content_digest}"
        artifact = create_artifact(
            artifact_id=row.artifact_id,
            version=row.version,
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec=qualified_spec,
            created_at=self._created_at(int(self._clock_ms())),
            user_id=row.user_id,
            parent=parent,
            task_origin=row.task_origin,
            source_digest=row.source_digest,
            state=ArtifactState.EPHEMERAL,
        )
        qualified_row = await self._store.persist_artifact(artifact)
        candidate = await self._candidate_with_reputation(qualified_row)
        return PackagedMcpQualification(
            artifact_digest=qualified_row.content_digest,
            candidate=candidate,
            protocol_version=protocol_version,
            server_name=server_name,
            server_version=server_version,
            attestation=dict(result.metadata.get("attestation") or {}),
            projected_match=tuple(item for item in required if item in available),
        )

    async def invoke_packaged(
        self,
        row,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        lease: CapabilityLease,
        timeout_ms: int,
    ) -> ActionResult:
        if self._package_runner is None:
            raise RemoteMcpError("packaged MCP gVisor runner is unavailable")
        if row.kind != ArtifactKind.MCP_ADAPTER.value or row.state != ArtifactState.QUALIFIED.value:
            raise PermissionError("packaged MCP adapter is not qualified")
        spec = dict(row.spec or {})
        package = spec.get("package") if isinstance(spec.get("package"), dict) else {}
        if self._package_kind(spec) != "mcpb" or package.get("transport") != "stdio-gvisor":
            raise PermissionError("MCP adapter is not a qualified packaged MCP")
        result = self._package_runner(
            runtime_class=RuntimeClass.GVISOR,
            artifact=row,
            lease=lease,
            inputs={
                "mode": "call",
                "protocolVersion": str(spec.get("protocolVersion") or "2025-06-18"),
                "toolName": str(tool_name),
                "arguments": dict(arguments),
            },
            timeout_ms=int(timeout_ms),
        )
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, ActionResult) or not isinstance(result.output, dict):
            raise TypeError("packaged MCP runner returned an invalid result")
        live_tools = self._packaged_live_tools(result.output)
        expected_tools = tuple(
            (str(item.get("name") or ""), str(item.get("inputSchemaDigest") or ""))
            for item in spec.get("tools") or ()
            if isinstance(item, dict)
        )
        observed_tools = tuple((item.name, item.input_schema_digest) for item in live_tools)
        if observed_tools != expected_tools:
            raise PermissionError("packaged MCP identity changed; reacquisition is required")
        payload = result.output.get("result")
        if not isinstance(payload, dict):
            raise RemoteMcpError("packaged MCP tool returned an invalid result")
        encoded = canonical_json(payload)
        if len(encoded) > 1_048_576:
            raise RemoteMcpError("packaged MCP result exceeds the response-size bound")
        return ActionResult(
            output=json.loads(encoded.decode("utf-8")),
            verification_passed=not bool(payload.get("isError", False)),
            metadata={
                **dict(result.metadata),
                "transport": "stdio-gvisor",
                "tool": str(tool_name),
                "packageDigest": package.get("sha256"),
            },
        )

    async def require_mount_candidate(
        self,
        row,
        *,
        goal: AcquisitionGoal,
        lease: CapabilityLease | None = None,
    ) -> tuple[McpCandidate, str | None]:
        if row.kind != ArtifactKind.MCP_ADAPTER.value:
            raise ValueError("MCP mount requires an McpAdapter artifact")
        if row.task_origin != goal.task_id:
            raise PermissionError("MCP adapter belongs to a different acquisition task")
        if row.state != ArtifactState.QUALIFIED.value:
            raise PermissionError("MCP adapter is not qualified for mounting")
        spec = dict(row.spec or {})
        package = spec.get("package") if isinstance(spec.get("package"), dict) else {}
        if package:
            if (
                self._package_kind(spec) != "mcpb"
                or package.get("transport") != "stdio-gvisor"
                or package.get("executable") is not True
                or not row.source_digest
                or str(package.get("bundleDigest") or "") != row.source_digest
            ):
                raise PermissionError("packaged MCP adapter is not sandbox-qualified")
            candidate = await self._candidate_with_reputation(row)
            qualification = self._fabric.qualify(candidate)
            if not qualification.eligible:
                raise PermissionError("packaged MCP adapter failed server qualification")
            available = set(candidate.tools)
            for tool in goal.required:
                if tool not in available:
                    raise PermissionError(
                        "packaged MCP adapter no longer exposes a required projected tool"
                    )
            return candidate, None
        remote = spec.get("remote") if isinstance(spec.get("remote"), dict) else {}
        remote_url = str(remote.get("url") or "").strip()
        if remote.get("transport") != "streamable-http" or not remote_url:
            raise PermissionError("MCP adapter transport is unsupported")
        auth = self._remote_auth(spec)
        credential_name = str(auth.get("logicalName") or "").strip() if auth else ""
        if credential_name:
            if self._credential_broker is None or lease is None:
                raise PermissionError(
                    "authenticated remote MCP mount requires credential authority"
                )
            _url, expected_name, _consumer = self.remote_auth_details(row)
            if credential_name != expected_name:
                raise PermissionError("remote MCP credential binding changed")
            observation = await self._connector.probe(
                remote_url,
                credential_broker=self._credential_broker,
                credential_lease=lease,
                credential_name=credential_name,
            )
        else:
            observation = await self._connector.probe(remote_url)
        expected = str(spec.get("identityFingerprint") or "")
        if not expected or observation.fingerprint != expected:
            raise PermissionError("remote MCP identity changed; reacquisition is required")
        candidate = await self._candidate_with_reputation(row)
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
        packaged_invoke: Any = None,
        credential_broker: CredentialBroker | None = None,
        evidence: EvidenceService | None = None,
    ) -> None:
        self._base = base_executor
        self._store = store
        self._authority = authority
        self._connector = connector
        self._packaged_invoke = packaged_invoke
        self._credential_broker = credential_broker
        self._evidence = evidence

    async def _record_outcome(
        self,
        *,
        mount,
        server_id: str,
        tool: str,
        transport: str,
        success: bool,
        verification_passed: bool | None = None,
        error_type: str | None = None,
    ) -> None:
        if self._evidence is None:
            return
        claims: dict[str, Any] = {
            "serverId": server_id,
            "tool": tool,
            "transport": transport,
            "success": bool(success),
        }
        if verification_passed is not None:
            claims["verificationPassed"] = bool(verification_passed)
        if error_type:
            claims["errorType"] = str(error_type)[:160]
        await self._evidence.record(
            task_id=mount.task_id,
            kind="mcp.invoke.outcome",
            producer_identity="capability-os-control",
            claims=claims,
            artifact_digest=mount.digest,
            lease_id=mount.lease_id,
        )

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
        package = spec.get("package") if isinstance(spec.get("package"), dict) else {}
        remote_url = str(remote.get("url") or "").strip()
        server_id = str(spec.get("serverId") or "").strip()
        if not server_id:
            raise PermissionError("projected MCP adapter identity is incomplete")
        packaged = package.get("transport") == "stdio-gvisor"
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
        auth = McpAcquisitionService._remote_auth(spec)
        credential_name = str(auth.get("logicalName") or "").strip() if auth else ""
        mount_lease = await self._authority.require_active(
            mount.lease_id,
            task_id=mount.task_id,
            artifact_digest=mount.digest,
            required_permissions=(required,),
            runtime_profile="remote-mcp",
            network_destinations=(() if packaged else (remote_url,)),
            credential_names=((credential_name,) if credential_name else ()),
        )
        if packaged:
            if self._packaged_invoke is None:
                raise PermissionError("packaged MCP runtime is unavailable")
            try:
                result = self._packaged_invoke(
                    artifact=artifact,
                    tool_name=tool,
                    arguments=dict(inputs),
                    task_id=mount.task_id,
                    timeout_ms=int(timeout_ms),
                )
                if asyncio.iscoroutine(result):
                    result = await result
                if not isinstance(result, ActionResult):
                    raise TypeError("packaged MCP executor returned an invalid result")
            except Exception as exc:
                await self._record_outcome(
                    mount=mount,
                    server_id=server_id,
                    tool=tool,
                    transport="stdio-gvisor",
                    success=False,
                    error_type=exc.__class__.__name__,
                )
                raise
            await self._record_outcome(
                mount=mount,
                server_id=server_id,
                tool=tool,
                transport="stdio-gvisor",
                success=bool(result.verification_passed),
                verification_passed=bool(result.verification_passed),
            )
            return result
        if not remote_url:
            raise PermissionError("remote MCP adapter identity is incomplete")
        invocation = (
            self._connector.invoke(
                remote_url=remote_url,
                tool_name=tool,
                arguments=dict(inputs),
                credential_broker=self._credential_broker,
                credential_lease=mount_lease,
                credential_name=credential_name,
            )
            if credential_name
            else self._connector.invoke(
                remote_url=remote_url,
                tool_name=tool,
                arguments=dict(inputs),
            )
        )
        try:
            result = await asyncio.wait_for(
                invocation,
                timeout=max(0.001, int(timeout_ms) / 1000),
            )
        except Exception as exc:
            await self._record_outcome(
                mount=mount,
                server_id=server_id,
                tool=tool,
                transport="streamable-http",
                success=False,
                error_type=exc.__class__.__name__,
            )
            raise
        await self._record_outcome(
            mount=mount,
            server_id=server_id,
            tool=tool,
            transport="streamable-http",
            success=bool(result.verification_passed),
            verification_passed=bool(result.verification_passed),
        )
        return result
