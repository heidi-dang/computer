"""Privileged-side validation engine for the Capability OS sandbox broker.

This module contains no ambient shell passthrough. It re-verifies immutable
source bundles, stages them into a private broker-owned tree, validates peer
identity and network/runtime prerequisites, and fails closed until an approved
runtime adapter is configured.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from cptr.services.capability_os.sandbox_broker import (
    BROKER_PROTOCOL_CURRENT,
    BROKER_PROTOCOL_V1,
    BROKER_PROTOCOL_V2,
    BrokerProtocolError,
    SandboxRequest,
)


class SandboxRuntimeUnavailable(RuntimeError):
    pass


@dataclass
class StagedBundle:
    source_dir: Path
    bundle_digest: str

    def cleanup(self) -> None:
        shutil.rmtree(self.source_dir.parent, ignore_errors=True)


class BundleStore:
    def __init__(self, *, spool_root: Path | str, state_root: Path | str,
                 expected_uid: int, expected_gid: int, max_bundle_bytes: int = 4 * 1024 * 1024) -> None:
        self.spool_root = Path(spool_root)
        self.state_root = Path(state_root)
        self.expected_uid = int(expected_uid)
        self.expected_gid = int(expected_gid)
        self.max_bundle_bytes = int(max_bundle_bytes)
        if not 1024 <= self.max_bundle_bytes <= 64 * 1024 * 1024:
            raise ValueError("sandbox bundle bound is outside permitted range")

    def _bundle_path(self, digest: str) -> Path:
        return self.spool_root / "sha256" / f"{digest.split(chr(58), 1)[1]}.json"

    def _read_verified(self, digest: str) -> dict[str, str]:
        path = self._bundle_path(digest)
        try:
            st = os.lstat(path)
        except FileNotFoundError as exc:
            raise BrokerProtocolError("sandbox bundle is unavailable") from exc
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise BrokerProtocolError("sandbox bundle must be a regular non-symlink file")
        if st.st_uid != self.expected_uid or st.st_gid != self.expected_gid:
            raise BrokerProtocolError("sandbox bundle ownership mismatch")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise BrokerProtocolError("sandbox bundle permissions are too broad")
        if st.st_size <= 0 or st.st_size > self.max_bundle_bytes:
            raise BrokerProtocolError("sandbox bundle size is outside permitted bounds")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise BrokerProtocolError("sandbox bundle cannot be opened safely") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise BrokerProtocolError("sandbox bundle changed type while being opened")
            if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                raise BrokerProtocolError("sandbox bundle changed while being opened")
            chunks: list[bytes] = []
            remaining = self.max_bundle_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(fd)
        if len(raw) != st.st_size or len(raw) > self.max_bundle_bytes:
            raise BrokerProtocolError("sandbox bundle changed while being read")
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        if actual != digest:
            raise BrokerProtocolError("sandbox bundle digest verification failed")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerProtocolError("sandbox bundle is not valid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"files"} or not isinstance(payload["files"], dict):
            raise BrokerProtocolError("sandbox bundle has invalid structure")
        files: dict[str, str] = {}
        for raw_path, content in payload["files"].items():
            rel = PurePosixPath(str(raw_path).replace("\\", "/"))
            if rel.is_absolute() or not rel.parts or ".." in rel.parts or rel.as_posix() in {".", ".."}:
                raise BrokerProtocolError("sandbox bundle contains unsafe path")
            if not isinstance(content, str):
                raise BrokerProtocolError("sandbox bundle content must be UTF-8 text")
            files[rel.as_posix()] = content
        return files

    def stage(self, request: SandboxRequest) -> StagedBundle:
        files = self._read_verified(request.bundle_digest)
        if request.entrypoint not in files:
            raise BrokerProtocolError("sandbox entrypoint is missing from immutable bundle")
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o710)
        state_st = os.lstat(self.state_root)
        expected_owner = 0 if os.geteuid() == 0 else os.geteuid()
        if (
            not stat.S_ISDIR(state_st.st_mode)
            or stat.S_ISLNK(state_st.st_mode)
            or state_st.st_uid != expected_owner
            or state_st.st_gid != self.expected_gid
        ):
            raise BrokerProtocolError("sandbox state root ownership mismatch")
        os.chmod(self.state_root, 0o710)
        root = Path(tempfile.mkdtemp(prefix="cptr-sbox-", dir=self.state_root))
        root_st = os.lstat(root)
        if root_st.st_gid != self.expected_gid:
            shutil.rmtree(root, ignore_errors=True)
            raise BrokerProtocolError("sandbox staging group ownership mismatch")
        # The unprivileged rootless runtime gets traverse-only access through
        # the broker-owned parent. It cannot list the directory; the mounted
        # source subtree below is independently made read-only/traversable.
        os.chmod(root, 0o710)
        source = root / "source"
        source.mkdir(mode=0o700)
        try:
            for rel, content in files.items():
                target = source.joinpath(*PurePosixPath(rel).parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                resolved_parent = target.parent.resolve()
                if source.resolve() not in (resolved_parent, *resolved_parent.parents):
                    raise BrokerProtocolError("sandbox staging escaped private source root")
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            # The outer broker-owned directory is root:<sandbox-group> 0710:
            # rootless runsc can traverse the already-known source path without
            # being able to list the parent. Source contents remain read-only.
            for current, directories, filenames in os.walk(source):
                os.chmod(current, 0o755)
                for name in filenames:
                    os.chmod(Path(current) / name, 0o444)
            return StagedBundle(source_dir=source, bundle_digest=request.bundle_digest)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise


class SandboxBrokerEngine:
    def __init__(self, *, bundles: BundleStore, runsc_path: Path | str,
                 expected_peer_uid: int | None = None, expected_peer_gid: int | None = None,
                 egress_proxy_available: bool = False,
                 dispatcher: Callable[[SandboxRequest, StagedBundle], dict[str, Any]] | None = None) -> None:
        self.bundles = bundles
        self.runsc_path = Path(runsc_path)
        self.expected_peer_uid = bundles.expected_uid if expected_peer_uid is None else int(expected_peer_uid)
        self.expected_peer_gid = bundles.expected_gid if expected_peer_gid is None else int(expected_peer_gid)
        self.egress_proxy_available = bool(egress_proxy_available)
        self.dispatcher = dispatcher

    def peer_allowed(self, *, uid: int, gid: int) -> bool:
        return int(uid) == self.expected_peer_uid and int(gid) == self.expected_peer_gid

    def _gvisor_available(self) -> bool:
        return self.runsc_path.is_file() and os.access(self.runsc_path, os.X_OK)

    def status(self) -> dict[str, Any]:
        gvisor_installed = self._gvisor_available()
        gvisor_ready = gvisor_installed and self.dispatcher is not None
        return {
            "runtimes": {"gvisor": gvisor_ready, "microvm": False, "wasm": False},
            "installedRuntimes": {"gvisor": gvisor_installed, "microvm": False, "wasm": False},
            "egressAllowList": self.egress_proxy_available,
            "brokerProtocol": BROKER_PROTOCOL_CURRENT,
            "supportedBrokerProtocols": [BROKER_PROTOCOL_V1, BROKER_PROTOCOL_V2],
        }

    def handle(self, request: SandboxRequest) -> dict[str, Any]:
        if not isinstance(request, SandboxRequest):
            raise TypeError("sandbox broker requires SandboxRequest")
        if request.operation == "status":
            return self.status()
        if request.runtime_class != "gvisor":
            raise SandboxRuntimeUnavailable(f"sandbox runtime {request.runtime_class} is unavailable")
        if not self._gvisor_available():
            raise SandboxRuntimeUnavailable("gVisor runsc is not installed or executable")
        if self.dispatcher is None:
            raise SandboxRuntimeUnavailable("approved sandbox runtime dispatcher is not configured")
        if request.network.get("outbound") == "allow-list" and not self.egress_proxy_available:
            raise SandboxRuntimeUnavailable("allow-list egress enforcement is unavailable")
        staged = self.bundles.stage(request)
        try:
            result = self.dispatcher(request, staged)
            if not isinstance(result, dict):
                raise BrokerProtocolError("sandbox dispatcher returned invalid result")
            return result
        finally:
            staged.cleanup()
