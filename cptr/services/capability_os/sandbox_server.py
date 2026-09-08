"""Socket-activated server for the standalone Capability OS sandbox broker."""
from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import socket
import struct
from pathlib import Path
from typing import Any

from cptr.services.capability_os.runtime_identity import trusted_runsc_release
from cptr.services.capability_os.gvisor_dispatcher import GvisorDispatcher, RootfsIdentity
from cptr.services.capability_os.sandbox_broker import (
    BROKER_PROTOCOL_CURRENT,
    BROKER_PROTOCOL_V1,
    BROKER_PROTOCOL_V2,
    BrokerProtocolError,
    SandboxRequest,
)
from cptr.services.capability_os.sandbox_daemon import BundleStore, SandboxBrokerEngine, SandboxRuntimeUnavailable

MAX_FRAME_BYTES = 64 * 1024


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    out = bytearray()
    while len(out) < length:
        chunk = sock.recv(length - len(out))
        if not chunk:
            raise BrokerProtocolError("broker peer closed an incomplete frame")
        out.extend(chunk)
    return bytes(out)


def _peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise BrokerProtocolError("SO_PEERCRED is required for sandbox broker")
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


def _send(sock: socket.socket, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if not body or len(body) > MAX_FRAME_BYTES:
        raise BrokerProtocolError("sandbox broker response exceeds frame bound")
    sock.sendall(len(body).to_bytes(4, "big") + body)


def serve_connection(conn: socket.socket, engine: SandboxBrokerEngine) -> None:
    request_id = "unknown"
    try:
        _pid, uid, gid = _peer_credentials(conn)
        if not engine.peer_allowed(uid=uid, gid=gid):
            raise BrokerProtocolError("sandbox broker peer identity mismatch")
        length = int.from_bytes(_recv_exact(conn, 4), "big")
        if length <= 0 or length > MAX_FRAME_BYTES:
            raise BrokerProtocolError("sandbox broker request frame is invalid")
        raw = _recv_exact(conn, length)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerProtocolError("sandbox broker request is not valid JSON") from exc
        if isinstance(payload, dict):
            request_id = str(payload.get("requestId") or "unknown")
        request = SandboxRequest.from_dict(payload)
        request_id = request.request_id
        result = engine.handle(request)
        _send(conn, {"requestId": request_id, "ok": True, "result": result})
    except (BrokerProtocolError, SandboxRuntimeUnavailable, ValueError, TypeError, OSError) as exc:
        code = "RUNTIME_UNAVAILABLE" if isinstance(exc, SandboxRuntimeUnavailable) else "BROKER_PROTOCOL_ERROR"
        try:
            _send(conn, {"requestId": request_id, "ok": False,
                         "error": {"code": code, "message": str(exc)}})
        except OSError:
            pass
    finally:
        conn.close()


def _systemd_listener() -> socket.socket:
    try:
        listen_pid = int(os.environ.get("LISTEN_PID", "0"))
        listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError as exc:
        raise RuntimeError("invalid systemd socket activation environment") from exc
    if listen_pid != os.getpid() or listen_fds != 1:
        raise RuntimeError("sandbox broker requires exactly one systemd-activated socket")
    listener = socket.socket(fileno=os.dup(3))
    if listener.family != socket.AF_UNIX or listener.type & socket.SOCK_STREAM != socket.SOCK_STREAM:
        listener.close()
        raise RuntimeError("sandbox broker activation fd is not an AF_UNIX stream socket")
    return listener


def _gvisor_dispatcher_from_env(*, runsc_path: Path, state_root: Path,
                                expected_rootfs_uid: int = 0) -> GvisorDispatcher | None:
    fields = {
        "rootfs": os.environ.get("CPTR_SANDBOX_PYTHON_ROOTFS", "").strip(),
        "digest": os.environ.get("CPTR_SANDBOX_PYTHON_ROOTFS_DIGEST", "").strip(),
        "runtime_version": os.environ.get("CPTR_SANDBOX_PYTHON_RUNTIME_VERSION", "").strip(),
        "runsc_version": os.environ.get("CPTR_SANDBOX_EXPECTED_RUNSC_VERSION", "").strip(),
    }
    configured = [bool(value) for value in fields.values()]
    if not any(configured):
        return None
    if not all(configured):
        raise RuntimeError("gVisor Python qualification profile is only partially configured")
    systemd_slice = os.environ.get("CPTR_SANDBOX_SYSTEMD_SLICE", "cptr-sandbox.slice").strip()
    runtime_user = os.environ.get(
        "CPTR_SANDBOX_RUNTIME_USER",
        os.environ.get("CPTR_SANDBOX_PEER_USER", "cptr"),
    ).strip()
    runtime_group = os.environ.get(
        "CPTR_SANDBOX_RUNTIME_GROUP",
        os.environ.get("CPTR_SANDBOX_PEER_GROUP", "cptr"),
    ).strip()
    if not systemd_slice:
        raise RuntimeError("gVisor systemd slice must not be blank")
    if not runtime_user or not runtime_group:
        raise RuntimeError("gVisor runtime user/group must not be blank")
    try:
        runtime_uid = pwd.getpwnam(runtime_user).pw_uid
        runtime_gid = grp.getgrnam(runtime_group).gr_gid
    except KeyError as exc:
        raise RuntimeError("gVisor runtime user/group is unavailable") from exc
    actual_release = trusted_runsc_release(runsc_path, expected_uid=expected_rootfs_uid)
    if actual_release != fields["runsc_version"]:
        raise RuntimeError("configured gVisor runsc release does not match the qualified release")
    identity = RootfsIdentity(
        profile="python",
        path=Path(fields["rootfs"]),
        digest=fields["digest"],
        runtime_version=fields["runtime_version"],
    )
    dispatcher = GvisorDispatcher(
        runsc_path=runsc_path,
        rootfs={"python": identity},
        state_root=state_root / "gvisor",
        runsc_version=actual_release,
        systemd_slice=systemd_slice,
        runtime_user=runtime_user,
        runtime_group=runtime_group,
        runtime_uid=runtime_uid,
        runtime_gid=runtime_gid,
        expected_rootfs_uid=expected_rootfs_uid,
    )
    try:
        dispatcher.validate_configuration()
    except SandboxRuntimeUnavailable as exc:
        raise RuntimeError(f"gVisor qualification profile is invalid: {exc}") from exc
    return dispatcher


def _engine_from_env() -> SandboxBrokerEngine:
    user = os.environ.get("CPTR_SANDBOX_PEER_USER", "cptr")
    group = os.environ.get("CPTR_SANDBOX_PEER_GROUP", "cptr")
    uid = pwd.getpwnam(user).pw_uid
    gid = grp.getgrnam(group).gr_gid
    state_root = Path(os.environ.get("CPTR_SANDBOX_STATE_ROOT", "/run/cptr-sandbox"))
    runsc_path = Path(os.environ.get("CPTR_SANDBOX_RUNSC", "/usr/bin/runsc"))
    bundles = BundleStore(
        spool_root=Path(os.environ.get("CPTR_SANDBOX_SPOOL_ROOT", "/var/lib/cptr/data/capability-os/blobs")),
        state_root=state_root,
        expected_uid=uid,
        expected_gid=gid,
    )
    dispatcher = _gvisor_dispatcher_from_env(
        runsc_path=runsc_path,
        state_root=state_root,
        expected_rootfs_uid=0,
    )
    return SandboxBrokerEngine(
        bundles=bundles,
        runsc_path=runsc_path,
        expected_peer_uid=uid,
        expected_peer_gid=gid,
        egress_proxy_available=os.environ.get("CPTR_SANDBOX_EGRESS_PROXY_READY", "false").lower() in {"1", "true", "yes"},
        dispatcher=dispatcher,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps({
            "brokerProtocol": BROKER_PROTOCOL_CURRENT,
            "supportedBrokerProtocols": [BROKER_PROTOCOL_V1, BROKER_PROTOCOL_V2],
            "executionSource": "standalone-zipapp",
        }, sort_keys=True))
        return 0
    engine = _engine_from_env()
    listener = _systemd_listener()
    with listener:
        while True:
            conn, _ = listener.accept()
            serve_connection(conn, engine)


if __name__ == "__main__":
    raise SystemExit(main())
