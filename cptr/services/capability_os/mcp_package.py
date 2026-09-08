"""Strict preparation and sandbox execution helpers for self-contained MCPB packages.

Only a deliberately narrow package subset is accepted: UTF-8, self-contained
Python MCPB archives that need no package installation, host filesystem access,
ambient credentials, or outbound network.  The package itself never executes in
the control plane.  It is converted into the existing immutable sandbox bundle
format and later probed/invoked through the qualified gVisor broker.
"""
from __future__ import annotations

import hashlib
import io
import json
import stat
import textwrap
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from cptr.services.capability_os.forge import ContentAddressedBlobStore


class McpbPackageError(ValueError):
    pass


_BRIDGE_ENTRYPOINT = "__cptr_mcp_bridge.py"
_SUPPORTED_MANIFEST_VERSIONS = frozenset({"0.1", "0.2", "0.3", "0.4"})


@dataclass(frozen=True)
class PreparedMcpbPackage:
    bundle_digest: str
    bundle_uri: str
    manifest_digest: str
    manifest_version: str
    package_name: str
    package_version: str
    server_entrypoint: str
    file_count: int


# This bridge is CPTR-owned code.  It runs *inside* the same network-denied,
# capability-empty gVisor sandbox as the untrusted MCPB server.  The child gets
# a fresh allow-listed environment, never the broker/control-plane environment.
_MCP_STDIO_BRIDGE = textwrap.dedent(
    r'''
    import json
    import os
    import selectors
    import signal
    import subprocess
    import sys
    import time

    _SERVER_ENTRYPOINT = __SERVER_ENTRYPOINT__
    _SERVER_ARGS = __SERVER_ARGS__
    _MAX_MESSAGE_BYTES = 1024 * 1024
    _MAX_STDERR_BYTES = 64 * 1024

    def _fail(message):
        print(json.dumps({"ok": False, "error": str(message)[:2048]}, separators=(",", ":")))
        raise SystemExit(0)

    try:
        request = json.loads(os.environ.get("CPTR_TOOL_INPUT_JSON", "{}"))
    except Exception:
        _fail("invalid CPTR MCP bridge input")
    if not isinstance(request, dict):
        _fail("invalid CPTR MCP bridge input")
    mode = str(request.get("mode") or "")
    if mode not in {"probe", "call"}:
        _fail("unsupported CPTR MCP bridge mode")
    protocol = str(request.get("protocolVersion") or "2025-06-18")[:64]
    tool_name = str(request.get("toolName") or "")
    arguments = request.get("arguments") or {}
    if mode == "call" and (not tool_name or not isinstance(arguments, dict)):
        _fail("invalid MCP tool call")

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "PYTHONHOME": "/usr",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": "/tmp/pycache-child",
        "PYTHONPATH": "/work/server/lib:/work",
    }
    argv = ["/usr/bin/python3", "/work/" + _SERVER_ENTRYPOINT, *_SERVER_ARGS]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            cwd="/work",
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        _fail("MCP server could not start: " + exc.__class__.__name__)

    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
    stdout_buffer = bytearray()
    stderr_seen = bytearray()
    pending = {}
    deadline = time.monotonic() + 12.0

    def _send(payload):
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        if len(raw) > _MAX_MESSAGE_BYTES:
            _fail("MCP request exceeds bridge bound")
        proc.stdin.write(raw)
        proc.stdin.flush()

    def _handle_line(line):
        if len(line) > _MAX_MESSAGE_BYTES:
            _fail("MCP response exceeds bridge bound")
        try:
            value = json.loads(line.decode("utf-8"))
        except Exception:
            _fail("MCP server emitted non-JSON stdout")
        if not isinstance(value, dict):
            _fail("MCP server emitted invalid message")
        msg_id = value.get("id")
        if msg_id is not None:
            pending[str(msg_id)] = value

    def _recv(response_id):
        key = str(response_id)
        while time.monotonic() < deadline:
            if key in pending:
                return pending.pop(key)
            timeout = max(0.0, min(0.1, deadline - time.monotonic()))
            for event, _mask in sel.select(timeout):
                chunk = os.read(event.fileobj.fileno(), 65536)
                if not chunk:
                    try:
                        sel.unregister(event.fileobj)
                    except Exception:
                        pass
                    continue
                if event.data == "stderr":
                    room = _MAX_STDERR_BYTES - len(stderr_seen)
                    if room > 0:
                        stderr_seen.extend(chunk[:room])
                    continue
                stdout_buffer.extend(chunk)
                if len(stdout_buffer) > _MAX_MESSAGE_BYTES:
                    _fail("MCP stdout buffer exceeds bridge bound")
                while b"\n" in stdout_buffer:
                    line, _, rest = stdout_buffer.partition(b"\n")
                    stdout_buffer[:] = rest
                    if line.strip():
                        _handle_line(line)
            if proc.poll() is not None and key not in pending:
                _fail("MCP server exited before replying")
        _fail("MCP server response timed out")

    try:
        _send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol,
                "capabilities": {},
                "clientInfo": {"name": "cptr-capability-os", "version": "1"},
            },
        })
        initialized = _recv(1)
        if "error" in initialized or not isinstance(initialized.get("result"), dict):
            _fail("MCP initialize failed")
        init_result = initialized["result"]
        _send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        _send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = _recv(2)
        if "error" in listed or not isinstance(listed.get("result"), dict):
            _fail("MCP tools/list failed")
        tools = listed["result"].get("tools")
        if not isinstance(tools, list) or len(tools) > 128:
            _fail("MCP tool list is invalid or too large")
        safe_tools = []
        seen = set()
        for item in tools:
            if not isinstance(item, dict):
                _fail("MCP tool descriptor is invalid")
            name = str(item.get("name") or "")
            schema = item.get("inputSchema")
            if not name or len(name) > 256 or name in seen or not isinstance(schema, dict):
                _fail("MCP tool descriptor is invalid")
            seen.add(name)
            safe_tools.append({"name": name, "inputSchema": schema})
        base = {
            "ok": True,
            "protocolVersion": str(init_result.get("protocolVersion") or protocol)[:128],
            "serverInfo": init_result.get("serverInfo") if isinstance(init_result.get("serverInfo"), dict) else {},
            "tools": safe_tools,
        }
        if mode == "probe":
            print(json.dumps(base, separators=(",", ":"), ensure_ascii=False))
        else:
            if tool_name not in seen:
                _fail("requested MCP tool is not exposed by the live server")
            _send({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            })
            called = _recv(3)
            if "error" in called:
                result = {"isError": True, "error": called.get("error")}
            else:
                raw_result = called.get("result")
                if not isinstance(raw_result, dict):
                    _fail("MCP tools/call returned invalid result")
                result = raw_result
            print(json.dumps({**base, "result": result}, separators=(",", ":"), ensure_ascii=False))
    finally:
        try:
            sel.close()
        except Exception:
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
    '''
).strip() + "\n"


def _safe_relpath(raw: str, *, label: str) -> str:
    text = str(raw or "").replace("\\", "/").strip()
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or text in {".", ".."}:
        raise McpbPackageError(f"{label} is not a safe relative path")
    return path.as_posix()


def _server_args(server: dict[str, Any], entrypoint: str) -> list[str]:
    config = server.get("mcp_config")
    if config is None:
        return []
    if not isinstance(config, dict):
        raise McpbPackageError("MCPB server mcp_config must be an object")
    env = config.get("env") or {}
    if not isinstance(env, dict) or env:
        raise McpbPackageError("MCPB packages requiring configured environment variables remain quarantined")
    command = str(config.get("command") or "python").strip().lower()
    if command not in {"python", "python3", "/usr/bin/python3"}:
        raise McpbPackageError("MCPB Python server declares an unsupported command")
    raw_args = config.get("args")
    if raw_args is None:
        return []
    if not isinstance(raw_args, list) or len(raw_args) > 32:
        raise McpbPackageError("MCPB server arguments are invalid")
    normalized: list[str] = []
    skipped_entrypoint = False
    for raw in raw_args:
        if not isinstance(raw, str) or len(raw) > 512 or "\x00" in raw:
            raise McpbPackageError("MCPB server argument is invalid")
        if "${user_config." in raw or ("${" in raw and "${__dirname}" not in raw):
            raise McpbPackageError("MCPB package requires unresolved runtime configuration")
        value = raw.replace("${__dirname}/", "").replace("${__dirname}", "")
        value = value.lstrip("/") if raw.startswith("${__dirname}/") else value
        if value == entrypoint and not skipped_entrypoint:
            skipped_entrypoint = True
            continue
        normalized.append(value)
    return normalized


def _validate_manifest(manifest: Any, files: dict[str, str], *, expected_version: str | None) -> tuple[str, str, str, list[str]]:
    if not isinstance(manifest, dict):
        raise McpbPackageError("MCPB manifest must be an object")
    manifest_version = str(manifest.get("manifest_version") or "").strip()
    if manifest_version not in _SUPPORTED_MANIFEST_VERSIONS:
        raise McpbPackageError("unsupported MCPB manifest version")
    name = str(manifest.get("name") or "").strip()
    version = str(manifest.get("version") or "").strip()
    if not name or not version:
        raise McpbPackageError("MCPB manifest name and version are required")
    if expected_version and version != str(expected_version).strip():
        raise McpbPackageError("MCPB manifest version does not match registry version")
    server = manifest.get("server")
    if not isinstance(server, dict):
        raise McpbPackageError("MCPB manifest server is required")
    if str(server.get("type") or "").strip().lower() != "python":
        raise McpbPackageError("only self-contained Python MCPB servers are currently qualified")
    entrypoint = _safe_relpath(str(server.get("entry_point") or ""), label="MCPB entrypoint")
    if not entrypoint.lower().endswith(".py") or entrypoint not in files:
        raise McpbPackageError("MCPB Python entrypoint is unavailable")
    compatibility = manifest.get("compatibility") or {}
    if not isinstance(compatibility, dict):
        raise McpbPackageError("MCPB compatibility must be an object")
    platforms = compatibility.get("platforms")
    if platforms is not None:
        if not isinstance(platforms, list) or "linux" not in {str(item).lower() for item in platforms}:
            raise McpbPackageError("MCPB package is not declared compatible with Linux")
    user_config = manifest.get("user_config") or {}
    if not isinstance(user_config, dict):
        raise McpbPackageError("MCPB user_config must be an object")
    for item in user_config.values():
        if isinstance(item, dict) and bool(item.get("required")):
            raise McpbPackageError("MCPB packages requiring user configuration remain quarantined")
    args = _server_args(server, entrypoint)
    return manifest_version, name, version, [entrypoint, *args]


class McpbPackagePreparer:
    def __init__(
        self,
        *,
        blobs: ContentAddressedBlobStore,
        max_archive_bytes: int = 16 * 1024 * 1024,
        max_uncompressed_bytes: int = 3 * 1024 * 1024,
        max_files: int = 256,
    ) -> None:
        self._blobs = blobs
        self._max_archive_bytes = int(max_archive_bytes)
        self._max_uncompressed_bytes = int(max_uncompressed_bytes)
        self._max_files = int(max_files)
        if min(self._max_archive_bytes, self._max_uncompressed_bytes, self._max_files) <= 0:
            raise ValueError("MCPB preparation bounds must be positive")

    def prepare(self, content: bytes, *, expected_version: str | None = None) -> PreparedMcpbPackage:
        if not isinstance(content, bytes) or not content or len(content) > self._max_archive_bytes:
            raise McpbPackageError("MCPB archive is outside permitted size bounds")
        files: dict[str, str] = {}
        try:
            archive = zipfile.ZipFile(io.BytesIO(content), mode="r")
        except (zipfile.BadZipFile, ValueError) as exc:
            raise McpbPackageError("MCPB artifact is not a valid ZIP archive") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > self._max_files:
                raise McpbPackageError("MCPB archive contains too many entries")
            total = 0
            for info in infos:
                name = str(info.filename).replace("\\", "/")
                if info.is_dir():
                    continue
                path = _safe_relpath(name, label="MCPB archive path")
                if path in files:
                    raise McpbPackageError("MCPB archive contains duplicate paths")
                unix_mode = (int(info.external_attr) >> 16) & 0xFFFF
                if unix_mode and stat.S_ISLNK(unix_mode):
                    raise McpbPackageError("MCPB archive symlinks are not allowed")
                if info.flag_bits & 0x1:
                    raise McpbPackageError("encrypted MCPB entries are not allowed")
                if info.file_size < 0 or info.file_size > self._max_uncompressed_bytes:
                    raise McpbPackageError("MCPB entry exceeds uncompressed size bound")
                total += int(info.file_size)
                if total > self._max_uncompressed_bytes:
                    raise McpbPackageError("MCPB archive exceeds uncompressed size bound")
                try:
                    raw = archive.read(info)
                except (RuntimeError, zipfile.BadZipFile) as exc:
                    raise McpbPackageError("MCPB archive entry could not be verified") from exc
                if len(raw) != info.file_size:
                    raise McpbPackageError("MCPB archive entry changed size while reading")
                try:
                    files[path] = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise McpbPackageError(
                        "MCPB contains binary/native content; only text-only Python bundles are enabled"
                    ) from exc
        manifest_text = files.get("manifest.json")
        if manifest_text is None:
            raise McpbPackageError("MCPB manifest.json is missing")
        try:
            manifest = json.loads(manifest_text)
        except json.JSONDecodeError as exc:
            raise McpbPackageError("MCPB manifest.json is invalid") from exc
        manifest_version, name, version, execution = _validate_manifest(
            manifest, files, expected_version=expected_version
        )
        entrypoint, *server_args = execution
        bridge = _MCP_STDIO_BRIDGE.replace(
            "__SERVER_ENTRYPOINT__", json.dumps(entrypoint)
        ).replace("__SERVER_ARGS__", json.dumps(server_args, separators=(",", ":")))
        if _BRIDGE_ENTRYPOINT in files:
            raise McpbPackageError("MCPB package collides with reserved CPTR bridge path")
        files[_BRIDGE_ENTRYPOINT] = bridge
        bundle_digest, bundle_uri = self._blobs.put_files(files)
        manifest_digest = "sha256:" + hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
        return PreparedMcpbPackage(
            bundle_digest=bundle_digest,
            bundle_uri=bundle_uri,
            manifest_digest=manifest_digest,
            manifest_version=manifest_version,
            package_name=name,
            package_version=version,
            server_entrypoint=entrypoint,
            file_count=len(files),
        )


BRIDGE_ENTRYPOINT = _BRIDGE_ENTRYPOINT
