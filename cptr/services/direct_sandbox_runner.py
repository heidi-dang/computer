"""Trusted Linux namespace runner for direct code-block operations.

The API never executes user text directly. ``DirectExecutorManager`` starts this
module with fixed argv. This runner creates a private user, mount, PID, and
network namespace, constructs a small chroot containing read-only runtime
libraries plus a read-write workspace bind mount, then drops capabilities before
starting the selected interpreter.

It is intentionally Linux-only. Deployments that cannot offer these primitives
must configure a separate approved runner or leave code-block execution disabled.
"""

from __future__ import annotations

import argparse
import os
import resource
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_CODE_BYTES = 200_000
MAX_MEMORY_BYTES = 512 * 1024 * 1024
MAX_NODE_ADDRESS_SPACE_BYTES = 6 * 1024 * 1024 * 1024
MAX_NODE_OLD_SPACE_MIB = 256
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PROCESSES = 64
TIMEOUT_EXIT_CODE = 124


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a direct code block in an isolated namespace")
    parser.add_argument("--language", choices=("python", "javascript", "typescript", "bash"))
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    parser.add_argument("--isolated", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", help=argparse.SUPPRESS)
    parser.add_argument("--root", help=argparse.SUPPRESS)
    parser.add_argument("--health", action="store_true")
    return parser


def _run(argv: list[str], *, check: bool = True) -> None:
    subprocess.run(argv, check=check, stdin=subprocess.DEVNULL)


def _bind_mount(source: Path, target: Path, *, readonly: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.touch(exist_ok=True)
    _run(["/usr/bin/mount", "--bind", str(source), str(target)])
    if readonly:
        _run(["/usr/bin/mount", "-o", "remount,ro,bind", str(target)])


def _prepare_root(root: Path, workspace: Path, source: Path) -> None:
    _run(["/usr/bin/mount", "--make-rprivate", "/"])
    _run(["/usr/bin/mount", "-t", "tmpfs", "-o", "mode=755,size=768m", "tmpfs", str(root)])
    for name in (
        "usr",
        "lib",
        "lib64",
        "workspace",
        "input",
        "tmp",
        "dev",
        "proc",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "node-runtime").touch(exist_ok=True)
    (root / "bin").symlink_to("usr/bin")

    # Interpreter/runtime trees are exposed read-only; only the explicitly
    # authorized workspace is writable after chroot.
    for host_path in (Path("/usr"), Path("/lib"), Path("/lib64")):
        if host_path.exists():
            _bind_mount(host_path, root / host_path.relative_to("/"), readonly=True)
    _bind_mount(workspace, root / "workspace", readonly=False)
    _bind_mount(source, root / "input" / source.name, readonly=True)
    node_runtime = os.environ.get("CPTR_DIRECT_CODE_NODE_RUNTIME", "").strip()
    if node_runtime:
        node_path = Path(node_runtime).resolve()
        if not node_path.is_file():
            raise RuntimeError("configured Node runtime is unavailable")
        _bind_mount(node_path, root / "node-runtime", readonly=True)
    for device in ("null", "zero", "random", "urandom"):
        host_device = Path("/dev") / device
        if host_device.exists():
            _bind_mount(host_device, root / "dev" / device, readonly=False)
    _run(["/usr/bin/mount", "-t", "proc", "proc", str(root / "proc")])


def _runtime_command(language: str, source_name: str) -> list[str]:
    input_path = f"/input/{source_name}"
    if language == "python":
        return ["/usr/bin/python3", "-I", input_path]
    if language == "bash":
        return ["/usr/bin/bash", "--noprofile", "--norc", input_path]
    if not Path("/node-runtime").is_file():
        raise RuntimeError("JavaScript runtime is not installed in the sandbox image")
    command = ["/node-runtime", f"--max-old-space-size={MAX_NODE_OLD_SPACE_MIB}"]
    if language == "typescript":
        command.append("--experimental-strip-types")
    command.append(input_path)
    return command


def _apply_limits(timeout_seconds: int, language: str) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (timeout_seconds, timeout_seconds + 1))
    address_space = (
        MAX_NODE_ADDRESS_SPACE_BYTES if language in {"javascript", "typescript"} else MAX_MEMORY_BYTES
    )
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
    resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))


def _run_isolated(args: argparse.Namespace) -> int:
    if not args.source or not args.root:
        raise RuntimeError("isolated runner arguments missing")
    workspace = Path(args.workspace).resolve()
    source = Path(args.source).resolve()
    root = Path(args.root).resolve()
    if not workspace.is_dir() or not source.is_file():
        raise RuntimeError("sandbox workspace or source is unavailable")

    _prepare_root(root, workspace, source)
    os.chroot(root)
    os.chdir("/workspace")
    _apply_limits(args.timeout_seconds, args.language)
    command = [
        "/usr/bin/setpriv",
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        *_runtime_command(args.language, source.name),
    ]
    return subprocess.run(command, check=False, stdin=subprocess.DEVNULL).returncode


def _run_outer(args: argparse.Namespace) -> int:
    if sys.platform != "linux":
        raise RuntimeError("the built-in direct sandbox runner requires Linux")
    if not 1 <= args.timeout_seconds <= 60:
        raise RuntimeError("timeout must be between 1 and 60 seconds")
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        raise RuntimeError("workspace is unavailable")
    code = sys.stdin.buffer.read(MAX_CODE_BYTES + 1)
    if not code or len(code) > MAX_CODE_BYTES:
        raise RuntimeError("code block is empty or exceeds the configured limit")

    unshare = shutil.which("unshare")
    if not unshare:
        raise RuntimeError("unshare is not installed")
    suffix = {"python": ".py", "javascript": ".js", "typescript": ".ts", "bash": ".sh"}[args.language]
    with tempfile.TemporaryDirectory(prefix="cptr-direct-sandbox-") as temporary_directory:
        temporary = Path(temporary_directory)
        source = temporary / f"code{suffix}"
        source.write_bytes(code)
        source.chmod(0o600)
        root = temporary / "root"
        root.mkdir(mode=0o700)
        command = [
            unshare,
            "--user",
            "--map-root-user",
            "--mount",
            "--pid",
            "--fork",
            "--net",
            "--mount-proc",
            sys.executable,
            str(Path(__file__).resolve()),
            "--isolated",
            "--language",
            args.language,
            "--workspace",
            str(workspace),
            "--timeout-seconds",
            str(args.timeout_seconds),
            "--source",
            str(source),
            "--root",
            str(root),
        ]
        try:
            return subprocess.run(
                command, check=False, stdin=subprocess.DEVNULL, timeout=args.timeout_seconds + 5
            ).returncode
        except subprocess.TimeoutExpired:
            return TIMEOUT_EXIT_CODE


def main() -> int:
    args = _parser().parse_args()
    if args.health:
        return 0 if sys.platform == "linux" and shutil.which("unshare") else 1
    return _run_isolated(args) if args.isolated else _run_outer(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"direct sandbox runner rejected execution: {exc}", file=sys.stderr)
        raise SystemExit(125) from exc
