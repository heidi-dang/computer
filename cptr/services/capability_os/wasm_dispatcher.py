"""Bounded Wasmtime dispatcher for portable generated Capability OS tools.

Source stays in CPTR's immutable UTF-8 bundle store as WebAssembly text (WAT).
The dispatcher never preopens host directories or grants sockets. Runtime input
is supplied as one bounded canonical JSON document on stdin, and generated tools
must emit one JSON value on stdout.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import resource
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from cptr.services.capability_os.sandbox_broker import SandboxRequest
from cptr.services.capability_os.sandbox_daemon import SandboxRuntimeUnavailable, StagedBundle


_MIB = 1024 * 1024
_REQUIRED_RESOURCES = frozenset(
    {"cpuMillis", "memoryMiB", "diskMiB", "pids", "wallTimeMs", "maxOutputBytes"}
)
_HARD_MAX = {
    "cpuMillis": 300_000,
    "memoryMiB": 2_048,
    "diskMiB": 1_024,
    "pids": 64,
    "wallTimeMs": 600_000,
    "maxOutputBytes": 16 * 1024 * 1024,
}


def _limits(request: SandboxRequest) -> dict[str, int]:
    raw = dict(request.resources)
    missing = _REQUIRED_RESOURCES - set(raw)
    if missing:
        raise SandboxRuntimeUnavailable(
            "WASM request is missing required resource limit: " + sorted(missing)[0]
        )
    result: dict[str, int] = {}
    for key in _REQUIRED_RESOURCES:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SandboxRuntimeUnavailable(f"WASM resource {key} must be numeric")
        integer = int(value)
        if integer <= 0 or integer > _HARD_MAX[key]:
            raise SandboxRuntimeUnavailable(f"WASM resource {key} exceeds broker hard limit")
        result[key] = integer
    if request.timeout_ms > result["wallTimeMs"]:
        raise SandboxRuntimeUnavailable("WASM timeout exceeds leased wall-time limit")
    return result


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _preexec(limits: dict[str, int], *, runtime_uid: int, runtime_gid: int):
    def apply() -> None:
        cpu_seconds = max(1, math.ceil(limits["cpuMillis"] / 1000))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        memory = limits["memoryMiB"] * _MIB
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_NPROC, (limits["pids"], limits["pids"]))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        file_limit = max(4096, limits["diskMiB"] * _MIB)
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(runtime_gid)
            os.setuid(runtime_uid)
        elif os.geteuid() != runtime_uid or os.getegid() != runtime_gid:
            raise PermissionError(
                "WASM dispatcher process identity does not match runtime identity"
            )

    return apply


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    limits: dict[str, int],
    timeout_seconds: float,
    runtime_uid: int,
    runtime_gid: int,
    stdin_data: bytes = b"",
) -> tuple[int, bytes, bytes]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_data else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
        preexec_fn=_preexec(limits, runtime_uid=runtime_uid, runtime_gid=runtime_gid),
    )
    assert process.stdout is not None and process.stderr is not None
    if stdin_data:
        assert process.stdin is not None
        try:
            process.stdin.write(stdin_data)
            process.stdin.flush()
        finally:
            process.stdin.close()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout_seconds
    max_output = limits["maxOutputBytes"]
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill(process)
                raise SandboxRuntimeUnavailable("WASM execution exceeded wall-time limit")
            events = selector.select(timeout=min(0.1, remaining))
            if not events and process.poll() is not None:
                continue
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = stdout if key.data == "stdout" else stderr
                target.extend(chunk)
                if len(stdout) + len(stderr) > max_output:
                    _kill(process)
                    raise SandboxRuntimeUnavailable("WASM execution exceeded output limit")
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        _kill(process)
        raise SandboxRuntimeUnavailable("WASM execution exceeded wall-time limit") from exc
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return int(process.returncode or 0), bytes(stdout), bytes(stderr)


class WasmDispatcher:
    def __init__(self, *, wasmtime_path: Path | str, runtime_uid: int, runtime_gid: int) -> None:
        self.wasmtime_path = Path(wasmtime_path)
        self.runtime_uid = int(runtime_uid)
        self.runtime_gid = int(runtime_gid)
        if self.runtime_uid < 0 or self.runtime_gid < 0:
            raise ValueError("WASM runtime identity is invalid")

    def validate_configuration(self) -> str:
        try:
            st = os.lstat(self.wasmtime_path)
        except FileNotFoundError as exc:
            raise SandboxRuntimeUnavailable("wasmtime is unavailable") from exc
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise SandboxRuntimeUnavailable(
                "wasmtime path must be a regular non-symlink executable"
            )
        if not os.access(self.wasmtime_path, os.X_OK):
            raise SandboxRuntimeUnavailable("wasmtime path is not executable")
        if st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o022:
            raise SandboxRuntimeUnavailable(
                "wasmtime executable ownership or permissions are unsafe"
            )
        try:
            result = subprocess.run(
                [str(self.wasmtime_path), "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxRuntimeUnavailable("wasmtime version probe failed") from exc
        version = result.stdout.strip()
        if not version or len(version) > 160:
            raise SandboxRuntimeUnavailable("wasmtime version probe returned invalid output")
        return version

    def __call__(self, request: SandboxRequest, staged: StagedBundle) -> dict[str, Any]:
        if request.runtime_class != "wasm" or request.profile != "wasm":
            raise SandboxRuntimeUnavailable("WASM dispatcher received incompatible runtime profile")
        if request.network.get("outbound") != "deny" or request.network.get("destinations"):
            raise SandboxRuntimeUnavailable("WASM runtime supports network deny only")
        if not request.entrypoint.lower().endswith(".wat"):
            raise SandboxRuntimeUnavailable("WASM portable tools require a .wat entrypoint")
        entrypoint = staged.source_dir / request.entrypoint
        if not entrypoint.is_file() or entrypoint.is_symlink():
            raise SandboxRuntimeUnavailable("WASM entrypoint is unavailable")
        version = self.validate_configuration()
        limits = _limits(request)
        timeout = min(request.timeout_ms, limits["wallTimeMs"]) / 1000.0
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
        }
        input_bytes = json.dumps(
            request.inputs,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        entrypoint_digest = "sha256:" + hashlib.sha256(entrypoint.read_bytes()).hexdigest()
        source_digest = request.bundle_digest

        if request.operation == "build":
            if request.inputs:
                raise SandboxRuntimeUnavailable("WASM build does not accept runtime inputs")
            with tempfile.TemporaryDirectory(prefix="cptr-wasm-build-") as temp_dir:
                temp_path = Path(temp_dir)
                if os.geteuid() == 0:
                    os.chown(temp_path, self.runtime_uid, self.runtime_gid)
                os.chmod(temp_path, 0o700)
                compiled = temp_path / "module.cwasm"
                returncode, stdout, stderr = _run(
                    [str(self.wasmtime_path), "compile", str(entrypoint), "-o", str(compiled)],
                    cwd=staged.source_dir,
                    env=env,
                    limits=limits,
                    timeout_seconds=timeout,
                    runtime_uid=self.runtime_uid,
                    runtime_gid=self.runtime_gid,
                )
                if returncode != 0 or stdout:
                    detail = stderr.decode("utf-8", errors="replace")[-2048:].strip()
                    raise SandboxRuntimeUnavailable(
                        f"WASM build failed with status {returncode}: {detail}"
                    )
                artifact_bytes = compiled.read_bytes()
                artifact_digest = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
            output = None
        elif request.operation == "run":
            returncode, stdout, stderr = _run(
                [str(self.wasmtime_path), "run", str(entrypoint)],
                cwd=staged.source_dir,
                env=env,
                limits=limits,
                timeout_seconds=timeout,
                runtime_uid=self.runtime_uid,
                runtime_gid=self.runtime_gid,
                stdin_data=input_bytes,
            )
            if returncode != 0:
                detail = stderr.decode("utf-8", errors="replace")[-2048:].strip()
                raise SandboxRuntimeUnavailable(
                    f"WASM runtime exited with status {returncode}: {detail}"
                )
            try:
                output = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SandboxRuntimeUnavailable(
                    "WASM generated tool must emit exactly one valid JSON value on stdout"
                ) from exc
            artifact_digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "sourceDigest": source_digest,
                            "wasmtimeVersion": version,
                            "entrypoint": request.entrypoint,
                            "operation": "run",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
        else:
            raise SandboxRuntimeUnavailable("WASM dispatcher operation is not executable")

        attestation = {
            "runtimeClass": "wasm",
            "runtimeVersion": version,
            "profile": "wasm",
            "profileVersion": "cptr-wasmtime-wat/1",
            "entrypoint": request.entrypoint,
            "sourceDigest": source_digest,
            "entrypointDigest": entrypoint_digest,
            "runtimeUid": self.runtime_uid,
            "runtimeGid": self.runtime_gid,
            "network": "deny",
            "preopenedDirectories": [],
            "resources": limits,
            "resourceEnforcement": {
                "cpu": "RLIMIT_CPU + broker deadline",
                "memory": "RLIMIT_AS",
                "pids": "RLIMIT_NPROC",
                "files": "RLIMIT_NOFILE/RLIMIT_FSIZE",
                "output": "broker bounded pipes",
            },
            "stdoutDigest": "sha256:" + hashlib.sha256(stdout).hexdigest(),
            "stderrDigest": "sha256:" + hashlib.sha256(stderr).hexdigest(),
            "exitCode": 0,
        }
        return {
            "artifactDigest": artifact_digest,
            "attestation": attestation,
            **({"output": output} if request.operation == "run" else {}),
        }
