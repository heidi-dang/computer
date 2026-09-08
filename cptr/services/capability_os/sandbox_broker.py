"""Narrow client/protocol boundary for the privileged Capability OS sandbox broker.

The control plane never sends shell commands to this boundary. It sends immutable
artifact/bundle identities and a fixed runner profile over a locally verified
Unix-domain socket. The root-owned broker is responsible for mapping that request
to an approved sandbox runtime.
"""

from __future__ import annotations

import asyncio
import grp
import json
import os
import re
import socket
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


BROKER_PROTOCOL_V1 = "cptr-sandbox/1"
BROKER_PROTOCOL_V2 = "cptr-sandbox/2"
BROKER_PROTOCOL_CURRENT = BROKER_PROTOCOL_V2
_SUPPORTED_PROTOCOLS = frozenset({BROKER_PROTOCOL_V1, BROKER_PROTOCOL_V2})
_ALLOWED_OPERATIONS = frozenset({"status", "build", "run", "destroy"})
_ALLOWED_RUNTIMES = frozenset({"gvisor", "microvm", "wasm"})
_ALLOWED_PROFILES = frozenset({"python", "node", "shell", "rust", "go", "wasm"})
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class BrokerProtocolError(RuntimeError):
    pass


class BrokerUnavailable(BrokerProtocolError):
    pass


class BrokerRuntimeUnavailable(BrokerProtocolError):
    pass


def _safe_mapping(value: dict[str, Any], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BrokerProtocolError(f"{label} must be an object")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 16_384:
        raise BrokerProtocolError(f"{label} exceeds broker metadata bound")
    return json.loads(encoded)


@dataclass(frozen=True)
class SandboxRequest:
    operation: str
    task_id: str
    lease_id: str
    artifact_digest: str
    runtime_class: str
    bundle_digest: str
    profile: str
    entrypoint: str = "main.py"
    request_id: str = field(default_factory=lambda: f"sboxreq_{uuid.uuid4().hex}")
    timeout_ms: int = 30_000
    resources: dict[str, Any] = field(default_factory=dict)
    network: dict[str, Any] = field(default_factory=lambda: {"outbound": "deny", "destinations": []})
    inputs: dict[str, Any] = field(default_factory=dict)
    protocol_version: str = BROKER_PROTOCOL_CURRENT

    def __post_init__(self) -> None:
        operation = str(self.operation).strip().lower()
        runtime = str(self.runtime_class).strip().lower()
        profile = str(self.profile).strip().lower()
        entrypoint = str(self.entrypoint).strip().replace("\\", "/")
        protocol = str(self.protocol_version).strip()
        if protocol not in _SUPPORTED_PROTOCOLS:
            raise BrokerProtocolError("unsupported sandbox broker protocol")
        if operation not in _ALLOWED_OPERATIONS:
            raise BrokerProtocolError("unsupported sandbox broker operation")
        if runtime not in _ALLOWED_RUNTIMES:
            raise BrokerProtocolError("unsupported sandbox runtime class")
        if profile not in _ALLOWED_PROFILES:
            raise BrokerProtocolError("unsupported sandbox runner profile")
        entrypoint_path = PurePosixPath(entrypoint)
        if not entrypoint or entrypoint_path.is_absolute() or ".." in entrypoint_path.parts or entrypoint in {".", ".."}:
            raise BrokerProtocolError("invalid sandbox entrypoint")
        for name in ("task_id", "lease_id", "request_id"):
            value = str(getattr(self, name)).strip()
            if not _ID_RE.fullmatch(value):
                raise BrokerProtocolError(f"invalid {name}")
            object.__setattr__(self, name, value)
        for name in ("artifact_digest", "bundle_digest"):
            value = str(getattr(self, name)).strip().lower()
            if not _DIGEST_RE.fullmatch(value):
                raise BrokerProtocolError(f"invalid {name}")
            object.__setattr__(self, name, value)
        if not 100 <= int(self.timeout_ms) <= 600_000:
            raise BrokerProtocolError("sandbox timeout is outside permitted bounds")
        resources = _safe_mapping(self.resources, label="resources")
        network = _safe_mapping(self.network, label="network")
        inputs = _safe_mapping(self.inputs, label="inputs")
        if protocol == BROKER_PROTOCOL_V1 and inputs:
            raise BrokerProtocolError("sandbox broker protocol v1 does not support runtime inputs")
        outbound = str(network.get("outbound") or "deny")
        if outbound not in {"deny", "allow-list"}:
            raise BrokerProtocolError("invalid sandbox network policy")
        destinations = network.get("destinations") or []
        if not isinstance(destinations, list) or len(destinations) > 64:
            raise BrokerProtocolError("invalid sandbox network destinations")
        network = {"outbound": outbound, "destinations": [str(item) for item in destinations]}
        object.__setattr__(self, "protocol_version", protocol)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "runtime_class", runtime)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "entrypoint", entrypoint_path.as_posix())
        object.__setattr__(self, "timeout_ms", int(self.timeout_ms))
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "network", network)
        object.__setattr__(self, "inputs", inputs)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SandboxRequest":
        if not isinstance(value, dict):
            raise BrokerProtocolError("sandbox broker request must be an object")
        protocol = str(value.get("protocolVersion") or "").strip()
        if protocol not in _SUPPORTED_PROTOCOLS:
            raise BrokerProtocolError("unsupported sandbox broker protocol")
        expected = {
            "protocolVersion", "requestId", "operation", "taskId", "leaseId",
            "artifactDigest", "runtimeClass", "bundleDigest", "profile",
            "entrypoint", "timeoutMs", "resources", "network",
        }
        if protocol == BROKER_PROTOCOL_V2:
            expected.add("inputs")
        unknown = set(value) - expected
        if unknown:
            raise BrokerProtocolError("sandbox broker request contains unsupported fields")
        return cls(
            protocol_version=protocol,
            request_id=str(value.get("requestId") or ""),
            operation=str(value.get("operation") or ""),
            task_id=str(value.get("taskId") or ""),
            lease_id=str(value.get("leaseId") or ""),
            artifact_digest=str(value.get("artifactDigest") or ""),
            runtime_class=str(value.get("runtimeClass") or ""),
            bundle_digest=str(value.get("bundleDigest") or ""),
            profile=str(value.get("profile") or ""),
            entrypoint=str(value.get("entrypoint") or ""),
            timeout_ms=int(value.get("timeoutMs") or 0),
            resources=dict(value.get("resources") or {}),
            network=dict(value.get("network") or {}),
            inputs=dict(value.get("inputs") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "protocolVersion": self.protocol_version,
            "requestId": self.request_id,
            "operation": self.operation,
            "taskId": self.task_id,
            "leaseId": self.lease_id,
            "artifactDigest": self.artifact_digest,
            "runtimeClass": self.runtime_class,
            "bundleDigest": self.bundle_digest,
            "profile": self.profile,
            "entrypoint": self.entrypoint,
            "timeoutMs": self.timeout_ms,
            "resources": self.resources,
            "network": self.network,
        }
        if self.protocol_version == BROKER_PROTOCOL_V2:
            payload["inputs"] = self.inputs
        return payload


class SandboxBrokerClient:
    def __init__(
        self,
        *,
        socket_path: Path | str,
        expected_uid: int = 0,
        expected_gid: int | None = None,
        max_frame_bytes: int = 64 * 1024,
        connect_timeout_seconds: float = 2.0,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.expected_uid = int(expected_uid)
        self.expected_gid = None if expected_gid is None else int(expected_gid)
        self.max_frame_bytes = int(max_frame_bytes)
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        if not 1024 <= self.max_frame_bytes <= 1024 * 1024:
            raise ValueError("broker frame bound must be between 1 KiB and 1 MiB")
        if not 0.1 <= self.connect_timeout_seconds <= 30:
            raise ValueError("broker connect timeout is outside permitted bounds")

    def _resolve_expected_gid(self) -> int:
        if self.expected_gid is not None:
            return self.expected_gid
        try:
            return int(grp.getgrnam("cptr").gr_gid)
        except KeyError as exc:
            raise BrokerProtocolError("required cptr group does not exist") from exc

    def _verify_socket(self) -> None:
        try:
            st = os.lstat(self.socket_path)
        except FileNotFoundError as exc:
            raise BrokerUnavailable("sandbox broker socket is unavailable") from exc
        if not stat.S_ISSOCK(st.st_mode):
            raise BrokerProtocolError("sandbox broker endpoint is not a Unix socket")
        if stat.S_IMODE(st.st_mode) != 0o660:
            raise BrokerProtocolError("sandbox broker socket mode must be exactly 0660")
        expected_gid = self._resolve_expected_gid()
        if st.st_uid != self.expected_uid or st.st_gid != expected_gid:
            raise BrokerProtocolError("sandbox broker socket ownership mismatch")

    @staticmethod
    def _recv_exact(sock: socket.socket, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = int(length)
        while remaining:
            chunk = sock.recv(remaining)
            if not chunk:
                raise BrokerProtocolError("sandbox broker closed an incomplete frame")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _invoke_sync(self, request: SandboxRequest) -> dict[str, Any]:
        self._verify_socket()
        body = json.dumps(request.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(body) > self.max_frame_bytes:
            raise BrokerProtocolError("sandbox broker request exceeds frame bound")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.connect_timeout_seconds)
            try:
                client.connect(str(self.socket_path))
            except OSError as exc:
                raise BrokerUnavailable("sandbox broker connection failed") from exc
            # Connection establishment is intentionally short, but a legitimate
            # broker request may run for the full leased sandbox deadline. Do
            # not accidentally reuse the connect timeout while waiting for the
            # bounded build/run response.
            client.settimeout((request.timeout_ms / 1000.0) + 5.0)
            try:
                client.sendall(len(body).to_bytes(4, "big") + body)
                header = self._recv_exact(client, 4)
                length = int.from_bytes(header, "big")
                if length <= 0 or length > self.max_frame_bytes:
                    raise BrokerProtocolError("sandbox broker response frame is invalid")
                raw = self._recv_exact(client, length)
            except socket.timeout as exc:
                raise BrokerUnavailable("sandbox broker response exceeded request deadline") from exc
            except (BrokenPipeError, ConnectionResetError) as exc:
                # The server intentionally validates SO_PEERCRED before reading
                # a request. Unauthorized peers are disconnected immediately;
                # expose that fail-closed boundary as a stable protocol error
                # rather than leaking a transport-specific BrokenPipeError.
                raise BrokerProtocolError("sandbox broker rejected peer connection") from exc
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerProtocolError("sandbox broker response is not valid JSON") from exc
        if not isinstance(response, dict) or response.get("requestId") != request.request_id:
            raise BrokerProtocolError("sandbox broker response identity mismatch")
        if response.get("ok") is not True:
            error = response.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            code = str(error.get("code") or "") if isinstance(error, dict) else ""
            if code == "RUNTIME_UNAVAILABLE":
                raise BrokerRuntimeUnavailable(str(message or "sandbox runtime unavailable"))
            raise BrokerProtocolError(str(message or "sandbox broker rejected request"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise BrokerProtocolError("sandbox broker result must be an object")
        return result

    async def invoke(self, request: SandboxRequest) -> dict[str, Any]:
        if not isinstance(request, SandboxRequest):
            raise TypeError("sandbox broker request must be SandboxRequest")
        return await asyncio.to_thread(self._invoke_sync, request)
