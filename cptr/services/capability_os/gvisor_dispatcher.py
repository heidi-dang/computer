"""Production gVisor OCI dispatcher used only by the privileged sandbox broker."""
from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cptr.services.capability_os.runtime_identity import measured_rootfs_digest, trusted_runsc_release
from cptr.services.capability_os.sandbox_broker import SandboxRequest
from cptr.services.capability_os.sandbox_daemon import SandboxRuntimeUnavailable, StagedBundle


_REQUIRED_RESOURCES = frozenset({
    "cpuMillis", "memoryMiB", "diskMiB", "pids", "wallTimeMs", "maxOutputBytes"
})
_HARD_MAX = {
    "cpuMillis": 300_000,
    "memoryMiB": 2_048,
    "diskMiB": 1_024,
    "pids": 128,
    "wallTimeMs": 600_000,
    "maxOutputBytes": 16 * 1024 * 1024,
}
_DIGEST_PREFIX = "sha256:"
_MIB = 1024 * 1024
_RUNTIME_MEMORY_OVERHEAD_MIB = 192
_RUNTIME_TASK_FLOOR = 128
_RUNTIME_TASK_OVERHEAD = 64
_RUNTIME_TASK_HARD_MAX = 256
_RUNTIME_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class RootfsIdentity:
    profile: str
    path: Path
    digest: str
    runtime_version: str

    def __post_init__(self) -> None:
        path = Path(self.path)
        if not self.profile.strip():
            raise ValueError("rootfs profile must not be blank")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.digest):
            raise ValueError("rootfs identity requires a sha256 digest")
        if not self.runtime_version.strip():
            raise ValueError("rootfs runtime version must not be blank")
        object.__setattr__(self, "path", path)


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _kill_process_group(process: subprocess.Popen) -> None:
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


def _run_bounded(argv: list[str], *, cwd: Path, timeout_seconds: float,
                 max_output_bytes: int) -> BoundedProcessResult:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout_seconds
    overflow = False
    timed_out = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(timeout=min(0.1, remaining))
            if not events and process.poll() is not None:
                # Pipes may still contain buffered bytes; continue until EOF.
                continue
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = stdout if key.data == "stdout" else stderr
                target.extend(chunk)
                if len(stdout) + len(stderr) > max_output_bytes:
                    overflow = True
                    break
            if overflow:
                break
        if timed_out or overflow:
            _kill_process_group(process)
        else:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    if timed_out:
        raise SandboxRuntimeUnavailable("sandbox execution exceeded wall-time limit")
    if overflow:
        raise SandboxRuntimeUnavailable("sandbox execution exceeded output limit")
    return BoundedProcessResult(
        returncode=int(process.returncode or 0),
        stdout=bytes(stdout),
        stderr=bytes(stderr),
    )


def _resource_limits(request: SandboxRequest) -> dict[str, int]:
    raw = dict(request.resources)
    missing = _REQUIRED_RESOURCES - set(raw)
    if missing:
        raise SandboxRuntimeUnavailable(
            "sandbox request is missing required resource limit: " + sorted(missing)[0]
        )
    result: dict[str, int] = {}
    for key in _REQUIRED_RESOURCES:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SandboxRuntimeUnavailable(f"sandbox resource {key} must be numeric")
        integer = int(value)
        if integer <= 0 or integer > _HARD_MAX[key]:
            raise SandboxRuntimeUnavailable(f"sandbox resource {key} exceeds broker hard limit")
        result[key] = integer
    if request.timeout_ms > result["wallTimeMs"]:
        raise SandboxRuntimeUnavailable("sandbox request timeout exceeds leased wall-time limit")
    return result


class GvisorDispatcher:
    def __init__(
        self,
        *,
        runsc_path: Path | str,
        rootfs: dict[str, RootfsIdentity],
        state_root: Path | str,
        runsc_version: str,
        systemd_slice: str,
        runtime_user: str,
        runtime_group: str,
        runtime_uid: int,
        runtime_gid: int,
        systemd_run_path: Path | str = "/usr/bin/systemd-run",
        systemctl_path: Path | str = "/usr/bin/systemctl",
        expected_rootfs_uid: int = 0,
    ) -> None:
        self.runsc_path = Path(runsc_path)
        self.rootfs = dict(rootfs)
        self.state_root = Path(state_root)
        self.runsc_version = str(runsc_version).strip()
        self.systemd_slice = str(systemd_slice).strip()
        self.runtime_user = str(runtime_user).strip()
        self.runtime_group = str(runtime_group).strip()
        self.runtime_uid = int(runtime_uid)
        self.runtime_gid = int(runtime_gid)
        self.systemd_run_path = Path(systemd_run_path)
        self.systemctl_path = Path(systemctl_path)
        self.expected_rootfs_uid = int(expected_rootfs_uid)
        if not self.runsc_version:
            raise ValueError("runsc version must not be blank")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}\.slice", self.systemd_slice):
            raise ValueError("gVisor systemd slice is invalid")
        if not _RUNTIME_USER_RE.fullmatch(self.runtime_user) or not _RUNTIME_USER_RE.fullmatch(self.runtime_group):
            raise ValueError("gVisor runtime user/group identity is invalid")
        if self.runtime_uid < 0 or self.runtime_gid < 0:
            raise ValueError("gVisor runtime uid/gid must be non-negative")

    def _identity(self, profile: str) -> RootfsIdentity:
        identity = self.rootfs.get(profile)
        if identity is None:
            raise SandboxRuntimeUnavailable(f"gVisor profile {profile} is not qualified")
        return identity

    def _validate_runsc(self) -> None:
        actual = trusted_runsc_release(self.runsc_path, expected_uid=self.expected_rootfs_uid)
        if actual != self.runsc_version:
            raise SandboxRuntimeUnavailable("qualified runsc release mismatch")

    @staticmethod
    def _validate_root_executable(path: Path, *, label: str) -> None:
        try:
            st = os.lstat(path)
        except FileNotFoundError as exc:
            raise SandboxRuntimeUnavailable(f"qualified {label} executable is unavailable") from exc
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or not os.access(path, os.X_OK):
            raise SandboxRuntimeUnavailable(f"qualified {label} path is not an executable regular file")
        if st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o022:
            raise SandboxRuntimeUnavailable(f"qualified {label} ownership or permissions are unsafe")

    @staticmethod
    def _runtime_bounds(limits: dict[str, int]) -> tuple[int, int]:
        memory_max = (limits["memoryMiB"] + _RUNTIME_MEMORY_OVERHEAD_MIB) * _MIB
        tasks_max = min(
            _RUNTIME_TASK_HARD_MAX,
            max(_RUNTIME_TASK_FLOOR, limits["pids"] + _RUNTIME_TASK_OVERHEAD),
        )
        return memory_max, tasks_max

    def _validate_rootfs(self, identity: RootfsIdentity) -> None:
        actual = measured_rootfs_digest(identity.path, expected_uid=self.expected_rootfs_uid)
        if actual != identity.digest:
            raise SandboxRuntimeUnavailable("qualified rootfs digest mismatch")
        try:
            st = os.lstat(identity.path)
        except FileNotFoundError as exc:
            raise SandboxRuntimeUnavailable("qualified rootfs is unavailable") from exc
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise SandboxRuntimeUnavailable("qualified rootfs path is invalid")
        if st.st_uid != self.expected_rootfs_uid:
            raise SandboxRuntimeUnavailable("qualified rootfs ownership mismatch")
        if stat.S_IMODE(st.st_mode) & 0o022:
            raise SandboxRuntimeUnavailable("qualified rootfs is group/world writable")
        if identity.profile == "python":
            python = identity.path / "usr/bin/python3"
            try:
                py_st = os.lstat(python)
            except FileNotFoundError as exc:
                raise SandboxRuntimeUnavailable("qualified Python rootfs is incomplete") from exc
            if not stat.S_ISREG(py_st.st_mode) or stat.S_ISLNK(py_st.st_mode) or not os.access(python, os.X_OK):
                raise SandboxRuntimeUnavailable("qualified Python interpreter is not an executable regular file")

    def validate_configuration(self) -> None:
        self._validate_runsc()
        self._validate_root_executable(self.systemd_run_path, label="systemd-run")
        self._validate_root_executable(self.systemctl_path, label="systemctl")
        if not self.rootfs:
            raise SandboxRuntimeUnavailable("gVisor dispatcher has no qualified rootfs profiles")
        for identity in self.rootfs.values():
            self._validate_rootfs(identity)

    def build_oci_config(self, request: SandboxRequest, staged: StagedBundle) -> dict[str, Any]:
        if request.runtime_class != "gvisor":
            raise SandboxRuntimeUnavailable("gVisor dispatcher received another runtime class")
        if request.network.get("outbound") != "deny" or request.network.get("destinations"):
            raise SandboxRuntimeUnavailable("gVisor profile currently supports network deny only")
        limits = _resource_limits(request)
        identity = self._identity(request.profile)
        self._validate_rootfs(identity)
        if request.profile != "python":
            raise SandboxRuntimeUnavailable("only the qualified Python gVisor profile is enabled")
        if request.operation == "build":
            if request.inputs:
                raise SandboxRuntimeUnavailable("sandbox build does not accept runtime inputs")
            args = ["/usr/bin/python3", "-m", "py_compile", f"/work/{request.entrypoint}"]
        elif request.operation == "run":
            args = ["/usr/bin/python3", f"/work/{request.entrypoint}"]
        else:
            raise SandboxRuntimeUnavailable("gVisor dispatcher operation is not executable")
        cpu_seconds = max(1, math.ceil(limits["cpuMillis"] / 1000))
        disk_mib = limits["diskMiB"]
        return {
            "ociVersion": "1.0.0",
            "process": {
                "terminal": False,
                # Rootless runsc maps the unprivileged host runtime identity to
                # namespace-local root. Guest capabilities remain completely empty.
                "user": {"uid": 0, "gid": 0},
                "args": args,
                "env": [
                    "PATH=/usr/bin:/bin",
                    "HOME=/tmp",
                    "PYTHONHOME=/usr",
                    "PYTHONDONTWRITEBYTECODE=1",
                    "PYTHONPYCACHEPREFIX=/tmp/pycache",
                    "CPTR_TOOL_INPUT_JSON=" + json.dumps(
                        request.inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ),
                ],
                "cwd": "/work",
                "noNewPrivileges": True,
                "capabilities": {
                    "bounding": [], "effective": [], "inheritable": [],
                    "permitted": [], "ambient": [],
                },
                "rlimits": [
                    {"type": "RLIMIT_NOFILE", "hard": 64, "soft": 64},
                    {"type": "RLIMIT_NPROC", "hard": limits["pids"], "soft": limits["pids"]},
                    {"type": "RLIMIT_CPU", "hard": cpu_seconds, "soft": cpu_seconds},
                    {"type": "RLIMIT_AS", "hard": limits["memoryMiB"] * _MIB,
                     "soft": limits["memoryMiB"] * _MIB},
                ],
            },
            "root": {"path": str(identity.path.resolve()), "readonly": True},
            "hostname": "cptr-sandbox",
            "mounts": [
                {"destination": "/proc", "type": "proc", "source": "proc"},
                {"destination": "/dev", "type": "tmpfs", "source": "tmpfs",
                 "options": ["nosuid", "noexec", "mode=0755", "size=16m"]},
                {"destination": "/sys", "type": "sysfs", "source": "sysfs",
                 "options": ["nosuid", "noexec", "nodev", "ro"]},
                {"destination": "/tmp", "type": "tmpfs", "source": "tmpfs",
                 "options": ["nosuid", "nodev", "mode=1777", f"size={disk_mib}m"]},
                {"destination": "/work", "type": "bind", "source": str(staged.source_dir.resolve()),
                 "options": ["rbind", "ro", "nosuid", "nodev"]},
            ],
            "linux": {
                # runsc is intentionally rootless with --ignore-cgroups. Exact
                # guest CPU/PID/address-space limits live in process rlimits;
                # runtime-wide memory/tasks are enforced by the parent systemd unit.
                "resources": {},
                "namespaces": [
                    {"type": "pid"}, {"type": "network"}, {"type": "ipc"},
                    {"type": "uts"}, {"type": "mount"},
                ],
            },
        }

    def _prepare_execution_root(self) -> tuple[Path, Path, Path, Path]:
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o710)
        expected_owner = 0 if os.geteuid() == 0 else os.geteuid()
        state_st = os.lstat(self.state_root)
        if (
            not stat.S_ISDIR(state_st.st_mode)
            or stat.S_ISLNK(state_st.st_mode)
            or state_st.st_uid != expected_owner
            or state_st.st_gid != self.runtime_gid
        ):
            raise SandboxRuntimeUnavailable("gVisor state root ownership mismatch")
        os.chmod(self.state_root, 0o710)
        execution_root = Path(tempfile.mkdtemp(prefix="gvisor-", dir=self.state_root))
        execution_st = os.lstat(execution_root)
        if execution_st.st_gid != self.runtime_gid:
            shutil.rmtree(execution_root, ignore_errors=True)
            raise SandboxRuntimeUnavailable("gVisor execution root group ownership mismatch")
        os.chmod(execution_root, 0o710)
        bundle = execution_root / "bundle"
        bundle.mkdir(mode=0o750)
        bundle_st = os.lstat(bundle)
        if bundle_st.st_gid != self.runtime_gid:
            shutil.rmtree(execution_root, ignore_errors=True)
            raise SandboxRuntimeUnavailable("gVisor bundle group ownership mismatch")
        # The broker service runs with UMask=0077, so mkdir(0750) would
        # otherwise become 0700 and prevent the unprivileged rootless runtime
        # from entering its OCI bundle. Restore the narrow group-read/traverse
        # mode explicitly after verifying the inherited runtime group.
        os.chmod(bundle, 0o750)
        probe_root = execution_root / "runsc-probe"
        exec_root = execution_root / "runsc-exec"
        for runtime_root in (probe_root, exec_root):
            runtime_root.mkdir(mode=0o770)
            runtime_st = os.lstat(runtime_root)
            if runtime_st.st_gid != self.runtime_gid:
                shutil.rmtree(execution_root, ignore_errors=True)
                raise SandboxRuntimeUnavailable("gVisor runtime root group ownership mismatch")
            os.chmod(runtime_root, 0o770)
        return execution_root, bundle, probe_root, exec_root

    def _write_config(self, bundle: Path, config: dict[str, Any]) -> None:
        path = bundle / "config.json"
        path.write_text(json.dumps(config, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        path_st = os.lstat(path)
        if path_st.st_gid != self.runtime_gid:
            raise SandboxRuntimeUnavailable("gVisor config group ownership mismatch")
        os.chmod(path, 0o640)

    def _transient_run_argv(
        self, *, unit_name: str, bundle: Path, runtime_root: Path,
        container_id: str, limits: dict[str, int], timeout_seconds: float,
    ) -> tuple[list[str], int, int]:
        memory_max, tasks_max = self._runtime_bounds(limits)
        runtime_max_seconds = max(1, math.ceil(timeout_seconds) + 2)
        argv = [
            str(self.systemd_run_path), "--quiet", "--wait", "--pipe", "--collect",
            f"--unit={unit_name}", f"--slice={self.systemd_slice}",
            f"--property=User={self.runtime_user}", f"--property=Group={self.runtime_group}",
            "--property=NoNewPrivileges=yes",
            # gVisor's systrap platform patches executable mappings. MDWE stays
            # enabled on the privileged broker but must be disabled in this
            # unprivileged, rootless transient runtime unit.
            "--property=MemoryDenyWriteExecute=no",
            "--property=RestrictSUIDSGID=yes", "--property=RestrictAddressFamilies=AF_UNIX",
            "--property=PrivateTmp=yes", "--property=ProtectSystem=strict",
            "--property=ProtectHome=yes", "--property=ProtectKernelTunables=yes",
            "--property=ProtectKernelModules=yes", "--property=ProtectControlGroups=yes",
            "--property=LockPersonality=yes", "--property=CapabilityBoundingSet=",
            "--property=AmbientCapabilities=", "--property=MemoryAccounting=yes",
            "--property=TasksAccounting=yes", f"--property=MemoryMax={memory_max}",
            f"--property=TasksMax={tasks_max}",
            f"--property=RuntimeMaxSec={runtime_max_seconds}s", "--property=TimeoutStopSec=2s",
            "--property=KillMode=mixed", "--property=UMask=0077",
            f"--property=WorkingDirectory={bundle}", f"--property=ReadWritePaths={runtime_root}",
            str(self.runsc_path), "--rootless=true", "--ignore-cgroups", "--network=none",
            "--platform=systrap", f"--root={runtime_root}", "run", f"--bundle={bundle}", container_id,
        ]
        return argv, memory_max, tasks_max

    def _stop_unit(self, unit_name: str) -> None:
        try:
            subprocess.run(
                [str(self.systemctl_path), "stop", f"{unit_name}.service"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _delete_rootless_container(self, runtime_root: Path, container_id: str, unit_name: str) -> None:
        cleanup_unit = f"{unit_name}-cleanup"
        argv = [
            str(self.systemd_run_path), "--quiet", "--wait", "--collect",
            f"--unit={cleanup_unit}", f"--slice={self.systemd_slice}",
            f"--property=User={self.runtime_user}", f"--property=Group={self.runtime_group}",
            "--property=NoNewPrivileges=yes", "--property=MemoryDenyWriteExecute=no",
            "--property=RestrictSUIDSGID=yes", "--property=RestrictAddressFamilies=AF_UNIX",
            "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
            "--property=ProtectControlGroups=yes", "--property=CapabilityBoundingSet=",
            "--property=AmbientCapabilities=", f"--property=ReadWritePaths={runtime_root}",
            str(self.runsc_path), "--rootless=true", "--ignore-cgroups",
            f"--root={runtime_root}", "delete", "--force", container_id,
        ]
        try:
            subprocess.run(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=4, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _cleanup(self, runtime_root: Path, container_id: str, unit_name: str) -> None:
        self._stop_unit(unit_name)
        self._delete_rootless_container(runtime_root, container_id, unit_name)
        null_netns = runtime_root / "null-netns"
        if os.path.ismount(null_netns):
            try:
                subprocess.run(
                    ["/bin/umount", "-l", str(null_netns)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=3, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

    def __call__(self, request: SandboxRequest, staged: StagedBundle) -> dict[str, Any]:
        self._validate_runsc()
        identity = self._identity(request.profile)
        config = self.build_oci_config(request, staged)
        limits = _resource_limits(request)
        execution_root, bundle, probe_root, exec_root = self._prepare_execution_root()
        identity_suffix = hashlib.sha256(request.request_id.encode()).hexdigest()[:20]
        probe_container = f"cptr-{identity_suffix}-probe"
        exec_container = f"cptr-{identity_suffix}-exec"
        probe_unit = f"cptr-sbox-{identity_suffix}-probe"
        exec_unit = f"cptr-sbox-{identity_suffix}-exec"
        deadline = time.monotonic() + min(request.timeout_ms, limits["wallTimeMs"]) / 1000.0
        runtime_memory_max, runtime_tasks_max = self._runtime_bounds(limits)
        try:
            version_config = json.loads(json.dumps(config))
            # A version-only probe can succeed even when the immutable stdlib
            # closure is missing. Import the minimum modules required by the
            # generated-tool protocol before accepting this rootfs/runtime pair.
            version_config["process"]["args"] = [
                "/usr/bin/python3",
                "-c",
                "import encodings,json,sys; print('Python ' + sys.version.split()[0])",
            ]
            self._write_config(bundle, version_config)
            probe_timeout = max(0.001, min(5.0, deadline - time.monotonic()))
            probe_argv, _probe_memory, _probe_tasks = self._transient_run_argv(
                unit_name=probe_unit, bundle=bundle, runtime_root=probe_root,
                container_id=probe_container, limits=limits, timeout_seconds=probe_timeout,
            )
            try:
                probe = _run_bounded(
                    probe_argv, cwd=bundle, timeout_seconds=probe_timeout, max_output_bytes=4096,
                )
            finally:
                self._cleanup(probe_root, probe_container, probe_unit)
            if probe.returncode != 0:
                detail = probe.stderr.decode("utf-8", errors="replace")[-2048:].strip()
                suffix = f": {detail}" if detail else ""
                raise SandboxRuntimeUnavailable(
                    f"gVisor qualification probe exited with status {probe.returncode}{suffix}"
                )
            if probe.stdout.decode("utf-8", errors="replace").strip() != identity.runtime_version:
                raise SandboxRuntimeUnavailable("qualified Python runtime version mismatch")

            self._write_config(bundle, config)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxRuntimeUnavailable("sandbox qualification exceeded wall-time limit")
            exec_argv, runtime_memory_max, runtime_tasks_max = self._transient_run_argv(
                unit_name=exec_unit, bundle=bundle, runtime_root=exec_root,
                container_id=exec_container, limits=limits, timeout_seconds=remaining,
            )
            try:
                result = _run_bounded(
                    exec_argv, cwd=bundle, timeout_seconds=remaining,
                    max_output_bytes=limits["maxOutputBytes"],
                )
            finally:
                self._cleanup(exec_root, exec_container, exec_unit)
            if len(result.stdout) + len(result.stderr) > limits["maxOutputBytes"]:
                raise SandboxRuntimeUnavailable("sandbox execution exceeded output limit")
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace")[-2048:]
                raise SandboxRuntimeUnavailable(
                    f"gVisor sandbox exited with status {result.returncode}: {detail}"
                )
            stdout_digest = _DIGEST_PREFIX + hashlib.sha256(result.stdout).hexdigest()
            stderr_digest = _DIGEST_PREFIX + hashlib.sha256(result.stderr).hexdigest()
            output: Any = None
            if request.operation == "run":
                try:
                    output = json.loads(result.stdout.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SandboxRuntimeUnavailable(
                        "generated tool must emit exactly one valid JSON value on stdout"
                    ) from exc
            identity_payload = {
                "sourceDigest": request.bundle_digest,
                "rootfsDigest": identity.digest,
                "runscVersion": self.runsc_version,
                "profile": request.profile,
                "profileVersion": "cptr-python-gvisor/2",
                "entrypoint": request.entrypoint,
                "operation": request.operation,
            }
            artifact_digest = _DIGEST_PREFIX + hashlib.sha256(
                json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return {
                "artifactDigest": artifact_digest,
                "attestation": {
                    **identity_payload,
                    "runtimeClass": "gvisor",
                    "runtimeVersion": identity.runtime_version,
                    "network": "deny",
                    "rootless": True,
                    "hostRuntimeUser": self.runtime_user,
                    "platform": "systrap",
                    "cgroupDriver": "systemd-transient",
                    "systemdSlice": self.systemd_slice,
                    "runscCgroups": "ignored-external-enforcement",
                    "runtimeMemoryMaxBytes": runtime_memory_max,
                    "runtimeTasksMax": runtime_tasks_max,
                    "resources": limits,
                    "resourceEnforcement": {
                        "guestCpu": "RLIMIT_CPU",
                        "guestPids": "RLIMIT_NPROC",
                        "guestMemory": "RLIMIT_AS",
                        "runtimeMemory": "systemd MemoryMax",
                        "runtimeTasks": "systemd TasksMax",
                        "disk": "tmpfs size",
                        "wallTime": "broker deadline + systemd RuntimeMaxSec",
                        "output": "broker bounded pipes",
                    },
                    "stdoutDigest": stdout_digest,
                    "stderrDigest": stderr_digest,
                    "exitCode": result.returncode,
                },
                **({"output": output} if request.operation == "run" else {}),
            }
        finally:
            self._cleanup(probe_root, probe_container, probe_unit)
            self._cleanup(exec_root, exec_container, exec_unit)
            shutil.rmtree(execution_root, ignore_errors=True)
