"""Structured, read-only FDX intelligence gateway for ChatGPT Direct Coding.

ChatGPT sees one MCP action and chooses an intelligence operation. CPTR owns
binary discovery, workspace/worker binding, protocol negotiation, process
lifecycle, path redaction, output bounds, and fallback semantics.

The gateway deliberately does not expose FDX mutation, verification execution,
attestation creation, policy mutation, test/lint execution, or raw shell args.
Those operations remain behind CPTR's existing purpose-built direct-coding
controls.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cptr.env import (
    DATA_DIR,
    FDX_BINARY,
    FDX_DAEMON_IDLE_TTL_SECONDS,
    FDX_ENABLED,
    FDX_MAX_DAEMONS,
    FDX_MAX_RESPONSE_BYTES,
    FDX_REQUEST_TIMEOUT_SECONDS,
)
from cptr.utils.identity import ExecutionIdentity, env_for, expand_user_path, preexec_for


FDX_DAEMON_PROTOCOL = 2
FDX_DAEMON_CAPABILITIES = (
    "read",
    "search",
    "outline",
    "impact-v1",
    "impact-v2",
    "why-v1",
    "evidence-graph-v1",
    "semantic-status-v1",
)
FDX_GATEWAY_ACTIONS = (
    "status",
    "capabilities",
    "read",
    "search",
    "grep",
    "batch",
    "outline",
    "tree",
    "ls",
    "impact",
    "impact_v2",
    "why",
    "evidence_graph",
    "semantic_status",
    "semantic_references",
    "build_status",
    "build_graph",
    "diff",
    "index_status",
    "plan",
)
_DAEMON_ACTION_TO_OP = {
    "read": "read",
    "search": "search",
    "outline": "outline",
    "impact": "impact",
    "impact_v2": "impact-v2",
    "why": "why-v1",
    "evidence_graph": "evidence-graph-v1",
    "semantic_status": "semantic-status-v1",
}
_REPOSITORY_BOUND_ACTIONS = frozenset(
    {
        "status",
        *_DAEMON_ACTION_TO_OP.keys(),
        "semantic_references",
        "build_status",
        "build_graph",
        "diff",
        "index_status",
        "plan",
    }
)
_REF_RE = re.compile(r"^[A-Za-z0-9._/@{}~^:+-]+$")
_UNIX_ABS_RE = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[A-Za-z0-9._~+-]+/)+[A-Za-z0-9._~+-]+")
_WINDOWS_ABS_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s\"'<>]+\\)*[^\\\s\"'<>]*")
_MAX_COLLECTION_ITEMS = 100
_MAX_STRING_CHARS = 24_000
_FDX_RAW_STREAM_LIMIT_BYTES = max(128 * 1024, FDX_MAX_RESPONSE_BYTES * 4)


class FdxIntelligenceError(RuntimeError):
    def __init__(self, code: str, message: str, *, retriable: bool = True):
        super().__init__(message)
        self.code = code
        self.retriable = retriable


class FdxUnavailable(FdxIntelligenceError):
    pass


async def _read_bounded_stream(
    stream: asyncio.StreamReader | None,
    *,
    limit: int,
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    retained = bytearray()
    total = 0
    too_large = False
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return bytes(retained), too_large
        total += len(chunk)
        remaining = max(0, limit - len(retained))
        if remaining:
            retained.extend(chunk[:remaining])
        if total > limit:
            too_large = True


@dataclass
class FdxDaemon:
    binary: str
    root: Path
    identity: ExecutionIdentity
    timeout_seconds: int
    process: asyncio.subprocess.Process | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stderr_task: asyncio.Task[None] | None = None
    stderr_tail: bytearray = field(default_factory=bytearray)
    last_used: float = field(default_factory=time.monotonic)
    negotiated: dict[str, Any] | None = None

    async def _drain_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        try:
            while True:
                chunk = await self.process.stderr.read(1024)
                if not chunk:
                    return
                self.stderr_tail.extend(chunk)
                if len(self.stderr_tail) > 8192:
                    del self.stderr_tail[:-8192]
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        environment = env_for(self.identity, self.root)
        kwargs: dict[str, Any] = {
            "cwd": str(self.root),
            "env": environment,
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "limit": _FDX_RAW_STREAM_LIMIT_BYTES,
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
            preexec = preexec_for(self.identity)
            if preexec is not None:
                kwargs["preexec_fn"] = preexec
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.binary,
                "serve",
                "--root",
                str(self.root),
                **kwargs,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise FdxUnavailable(
                "FDX_BINARY_UNAVAILABLE", "FDX native binary is unavailable"
            ) from exc
        self.stderr_tail.clear()
        self.stderr_task = asyncio.create_task(self._drain_stderr(), name="cptr-fdx-stderr")
        self.negotiated = None
        await self.negotiate()

    async def negotiate(self) -> dict[str, Any]:
        if self.negotiated is not None:
            return self.negotiated
        value = await self._request_unlocked(
            "negotiate",
            {
                "protocol": FDX_DAEMON_PROTOCOL,
                "capabilities": list(FDX_DAEMON_CAPABILITIES),
            },
        )
        protocol = int(value.get("protocol") or 0) if isinstance(value, dict) else 0
        if protocol != FDX_DAEMON_PROTOCOL:
            await self.stop()
            raise FdxIntelligenceError(
                "FDX_PROTOCOL_MISMATCH",
                f"FDX protocol mismatch: expected {FDX_DAEMON_PROTOCOL}, got {protocol or 'unknown'}",
                retriable=False,
            )
        self.negotiated = value
        return value

    async def request(self, op: str, args: dict[str, Any]) -> Any:
        async with self.lock:
            await self.start()
            self.last_used = time.monotonic()
            return await self._request_unlocked(op, args)

    async def _request_unlocked(self, op: str, args: dict[str, Any]) -> Any:
        process = self.process
        if (
            process is None
            or process.returncode is not None
            or process.stdin is None
            or process.stdout is None
        ):
            raise FdxUnavailable("FDX_DAEMON_UNAVAILABLE", "FDX daemon is not running")
        request_id = f"cptr-{uuid.uuid4().hex}"
        payload = json.dumps({"id": request_id, "op": op, "args": args}, separators=(",", ":"))
        try:
            process.stdin.write((payload + "\n").encode("utf-8"))
            await asyncio.wait_for(process.stdin.drain(), timeout=self.timeout_seconds)
            raw = await asyncio.wait_for(process.stdout.readline(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise FdxIntelligenceError("FDX_TIMEOUT", "FDX intelligence request timed out") from exc
        except ValueError as exc:
            await self.stop()
            raise FdxIntelligenceError(
                "FDX_RESPONSE_TOO_LARGE",
                "FDX daemon response exceeded the bounded CPTR transport limit",
            ) from exc
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            await self.stop()
            raise FdxIntelligenceError("FDX_DAEMON_IO", "FDX daemon connection failed") from exc
        if not raw:
            detail = self.stderr_tail.decode("utf-8", errors="replace").strip()
            await self.stop()
            raise FdxIntelligenceError(
                "FDX_DAEMON_EXITED",
                "FDX daemon exited before returning a response"
                + (f": {detail[:300]}" if detail else ""),
            )
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            await self.stop()
            raise FdxIntelligenceError(
                "FDX_INVALID_RESPONSE", "FDX returned malformed JSON"
            ) from exc
        if response.get("id") != request_id:
            await self.stop()
            raise FdxIntelligenceError("FDX_RESPONSE_MISMATCH", "FDX response correlation failed")
        if response.get("ok") is not True:
            error = str(response.get("error") or "FDX request failed")
            raise FdxIntelligenceError("FDX_OPERATION_FAILED", error[:500])
        return response.get("value")

    async def stop(self) -> None:
        process = self.process
        self.process = None
        self.negotiated = None
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
        if self.stderr_task is not None:
            self.stderr_task.cancel()
            try:
                await self.stderr_task
            except asyncio.CancelledError:
                pass
            self.stderr_task = None


class FdxIntelligenceService:
    def __init__(self) -> None:
        self._daemons: dict[tuple[str, str, str], FdxDaemon] = {}
        self._registry_lock = asyncio.Lock()

    def _resolve_binary(self, identity: ExecutionIdentity) -> str:
        if not FDX_ENABLED:
            raise FdxUnavailable(
                "FDX_DISABLED", "FDX intelligence is disabled by CPTR configuration"
            )
        environment = env_for(identity, Path(identity.home))
        candidates: list[Path | str] = []
        if FDX_BINARY:
            candidates.append(expand_user_path(FDX_BINARY, identity))
        executable_name = "fdx.exe" if os.name == "nt" else "fdx"
        candidates.append(Path(identity.home) / ".cptr" / "bin" / executable_name)
        candidates.append(Path(identity.home) / ".cargo" / "bin" / executable_name)
        candidates.append(Path(DATA_DIR) / "bin" / executable_name)
        for candidate in candidates:
            path = Path(candidate)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.resolve())
        on_path = shutil.which("fdx", path=environment.get("PATH"))
        if on_path:
            return str(Path(on_path).resolve())
        raise FdxUnavailable(
            "FDX_BINARY_UNAVAILABLE",
            "FDX native binary is not installed; use normal CPTR Direct Coding tools",
        )

    async def _daemon(
        self,
        *,
        user_id: str,
        workspace_id: str,
        root: Path,
        identity: ExecutionIdentity,
    ) -> FdxDaemon:
        binary = self._resolve_binary(identity)
        key = (user_id, workspace_id, str(root.resolve()))
        now = time.monotonic()
        stale: list[FdxDaemon] = []
        async with self._registry_lock:
            for existing_key, daemon in list(self._daemons.items()):
                if (
                    daemon.process is not None and daemon.process.returncode is not None
                ) or now - daemon.last_used >= FDX_DAEMON_IDLE_TTL_SECONDS:
                    stale.append(self._daemons.pop(existing_key))
            daemon = self._daemons.get(key)
            if daemon is None:
                if len(self._daemons) >= FDX_MAX_DAEMONS:
                    oldest_key, oldest = min(
                        self._daemons.items(), key=lambda item: item[1].last_used
                    )
                    stale.append(self._daemons.pop(oldest_key))
                daemon = FdxDaemon(
                    binary=binary,
                    root=root.resolve(),
                    identity=identity,
                    timeout_seconds=FDX_REQUEST_TIMEOUT_SECONDS,
                )
                self._daemons[key] = daemon
        for old in stale:
            await old.stop()
        return daemon

    async def close_all(self) -> None:
        async with self._registry_lock:
            daemons = list(self._daemons.values())
            self._daemons.clear()
        await asyncio.gather(*(daemon.stop() for daemon in daemons), return_exceptions=True)

    async def _run_cli(
        self,
        *,
        root: Path,
        identity: ExecutionIdentity,
        argv: list[str],
    ) -> Any:
        binary = self._resolve_binary(identity)
        environment = env_for(identity, root)
        kwargs: dict[str, Any] = {
            "cwd": str(root),
            "env": environment,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
            preexec = preexec_for(identity)
            if preexec is not None:
                kwargs["preexec_fn"] = preexec
        try:
            process = await asyncio.create_subprocess_exec(binary, *argv, **kwargs)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise FdxUnavailable(
                "FDX_BINARY_UNAVAILABLE", "FDX native binary is unavailable"
            ) from exc
        stdout_task = asyncio.create_task(
            _read_bounded_stream(
                process.stdout,
                limit=_FDX_RAW_STREAM_LIMIT_BYTES,
            )
        )
        stderr_task = asyncio.create_task(
            _read_bounded_stream(
                process.stderr,
                limit=_FDX_RAW_STREAM_LIMIT_BYTES,
            )
        )
        wait_task = asyncio.create_task(process.wait())
        try:
            stdout_result, stderr_result, _ = await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task, wait_task),
                timeout=FDX_REQUEST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.communicate(), timeout=2)
            except (asyncio.TimeoutError, RuntimeError):
                pass
            raise FdxIntelligenceError("FDX_TIMEOUT", "FDX intelligence request timed out") from exc
        finally:
            for task in (stdout_task, stderr_task, wait_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)
        stdout, stdout_too_large = stdout_result
        stderr, stderr_too_large = stderr_result
        if stdout_too_large or stderr_too_large:
            label = "stdout" if stdout_too_large else "stderr"
            raise FdxIntelligenceError(
                "FDX_RESPONSE_TOO_LARGE",
                f"FDX {label} exceeded the bounded CPTR transport limit",
            )
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise FdxIntelligenceError(
                "FDX_OPERATION_FAILED",
                (detail or f"FDX exited with code {process.returncode}")[:500],
            )
        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    @staticmethod
    def _validate_ref(value: str | None, field_name: str) -> str | None:
        if value is None or value == "":
            return None
        if value.startswith("-") or not _REF_RE.fullmatch(value):
            raise FdxIntelligenceError(
                "FDX_INVALID_GIT_REF",
                f"{field_name} is not a safe Git revision expression",
                retriable=False,
            )
        return value

    @staticmethod
    def _daemon_args(action: str, options: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if action == "read":
            path = options.get("path")
            if not path:
                raise FdxIntelligenceError(
                    "FDX_INVALID_ARGUMENT", "read requires path", retriable=False
                )
            args = {
                "path": path,
                "mode": options.get("mode") or "auto",
                "symbol": options.get("symbol"),
                "limit": options.get("limit"),
                "offset": options.get("offset"),
                "with_deps": options.get("with_deps", True),
                "no_cache": bool(options.get("no_cache")),
            }
        elif action == "search":
            args = {
                "pattern": options.get("query") or options.get("pattern"),
                "paths": options.get("paths")
                or ([options["path"]] if options.get("path") else ["."]),
                "kind": options.get("kind") or "any",
                "max_matches": options.get("max_matches") or 50,
            }
        elif action == "outline":
            args = {
                "paths": options.get("paths")
                or ([options["path"]] if options.get("path") else ["."]),
                "depth": options.get("depth"),
                "kind": options.get("kind"),
                "min_lines": options.get("min_lines") or 1,
            }
        elif action == "impact":
            args = {
                "paths": options.get("paths") or ([options["path"]] if options.get("path") else []),
                "depth": options.get("depth") or 1,
                "direction": options.get("direction") or "both",
            }
        elif action == "impact_v2":
            args = {
                "base": options.get("base"),
                "head": options.get("head"),
                "depth": options.get("depth") or 3,
            }
        elif action == "why":
            args = {
                "target": options.get("target"),
                "base": options.get("base"),
                "head": options.get("head"),
                "depth": options.get("depth") or 3,
            }
        return {key: value for key, value in args.items() if value is not None}

    @staticmethod
    def _cli_argv(action: str, options: dict[str, Any]) -> list[str]:
        path = options.get("path")
        paths = [str(item) for item in options.get("paths") or []]
        base = FdxIntelligenceService._validate_ref(options.get("base"), "base")
        head = FdxIntelligenceService._validate_ref(options.get("head"), "head")

        if action == "capabilities":
            return ["capabilities", "--contract-version", "1", "--format", "json"]
        if action == "read":
            if not path:
                raise FdxIntelligenceError(
                    "FDX_INVALID_ARGUMENT", "read requires path", retriable=False
                )
            argv = [
                "read",
                str(path),
                "--mode",
                str(options.get("mode") or "auto"),
                "--format",
                "json",
            ]
            if options.get("symbol"):
                argv += ["--symbol", str(options["symbol"])]
            if options.get("limit"):
                argv += ["--limit", str(options["limit"])]
            if options.get("offset"):
                argv += ["--offset", str(options["offset"])]
            if options.get("with_deps") is False:
                argv += ["--with-deps", "false"]
            if options.get("no_cache"):
                argv.append("--no-cache")
            return argv
        if action == "grep":
            pattern = options.get("query") or options.get("pattern")
            if not pattern:
                raise FdxIntelligenceError(
                    "FDX_INVALID_ARGUMENT", "grep requires query", retriable=False
                )
            argv = ["grep", str(pattern)] + (paths or ([str(path)] if path else ["."]))
            argv += [
                "--context",
                str(options.get("context") or 2),
                "--max-matches",
                str(options.get("max_matches") or 50),
                "--format",
                "json",
                "--no-tee",
            ]
            if options.get("fixed_strings"):
                argv.append("--fixed-strings")
            if options.get("case_sensitive"):
                argv.append("--case-sensitive")
            if options.get("no_cache"):
                argv.append("--no-cache")
            return argv
        if action == "batch":
            selected = paths or ([str(path)] if path else [])
            if not selected:
                raise FdxIntelligenceError(
                    "FDX_INVALID_ARGUMENT", "batch requires path or paths", retriable=False
                )
            argv = [
                "batch",
                *selected,
                "--mode",
                str(options.get("mode") or "prototype"),
                "--format",
                "json",
                "--max-files",
                str(options.get("max_files") or 20),
            ]
            if options.get("symbol"):
                argv += ["--symbol", str(options["symbol"])]
            if options.get("limit_per_file"):
                argv += ["--limit-per-file", str(options["limit_per_file"])]
            if options.get("no_cache"):
                argv.append("--no-cache")
            return argv
        if action == "tree":
            argv = [
                "tree",
                str(path or "."),
                "--depth",
                str(options.get("depth") or 3),
                "--format",
                "json",
            ]
            if options.get("dirs_only"):
                argv.append("--dirs-only")
            return argv
        if action == "ls":
            argv = ["ls", str(path or "."), "--format", "json"]
            if options.get("all"):
                argv.append("--all")
            return argv
        if action == "semantic_references":
            symbol = options.get("symbol") or options.get("query")
            if not symbol:
                raise FdxIntelligenceError(
                    "FDX_INVALID_ARGUMENT", "semantic_references requires symbol", retriable=False
                )
            return [
                "semantic",
                "references",
                str(symbol),
                "--lang",
                str(options.get("lang") or "rust"),
                "--intent",
                str(options.get("intent") or "reference_complete"),
            ]
        if action == "build_status":
            return ["build", "status"]
        if action == "build_graph":
            return ["build", "graph", "--format", "json"]
        if action == "diff":
            argv = ["diff"]
            if base:
                argv.append(base)
            if options.get("staged"):
                argv.append("--staged")
            argv += ["--format", "json", "--root", "."]
            if paths:
                argv += ["--", *paths]
            return argv
        if action == "index_status":
            return ["index", "status"]
        if action == "plan":
            argv = ["plan", "--format", "json"]
            if base:
                argv += ["--base", base]
            if head:
                argv += ["--head", head]
            if options.get("policy_overlay"):
                argv.append("--policy-overlay")
            return argv
        raise FdxIntelligenceError(
            "FDX_UNSUPPORTED_ACTION",
            f"unsupported FDX intelligence action: {action}",
            retriable=False,
        )

    @staticmethod
    def _sanitize_string(value: str, root: Path) -> str:
        resolved_root = root.resolve()
        try:
            exact_path = Path(value)
            if exact_path.is_absolute():
                resolved_path = exact_path.resolve()
                if resolved_path.is_relative_to(resolved_root):
                    return resolved_path.relative_to(resolved_root).as_posix() or "."
                return "<redacted-path>"
        except (OSError, ValueError):
            pass

        root_text = str(resolved_root)
        value = value.replace(root_text, ".")

        def unix_replace(match: re.Match[str]) -> str:
            raw = match.group(0)
            try:
                candidate = Path(raw).resolve()
                if candidate.is_relative_to(root.resolve()):
                    return candidate.relative_to(root.resolve()).as_posix() or "."
            except (OSError, ValueError):
                pass
            return "<redacted-path>"

        value = _UNIX_ABS_RE.sub(unix_replace, value)
        value = _WINDOWS_ABS_RE.sub("<redacted-path>", value)
        if len(value) > _MAX_STRING_CHARS:
            return value[:_MAX_STRING_CHARS] + "\n[FDX string truncated by CPTR]"
        return value

    @classmethod
    def _sanitize(cls, value: Any, root: Path, *, depth: int = 0) -> Any:
        if depth > 12:
            return "[FDX_MAX_DEPTH]"
        if isinstance(value, str):
            return cls._sanitize_string(value, root)
        if isinstance(value, list):
            items = [
                cls._sanitize(item, root, depth=depth + 1) for item in value[:_MAX_COLLECTION_ITEMS]
            ]
            if len(value) > _MAX_COLLECTION_ITEMS:
                items.append({"truncated_items": len(value) - _MAX_COLLECTION_ITEMS})
            return items
        if isinstance(value, dict):
            return {
                str(key)[:200]: cls._sanitize(item, root, depth=depth + 1)
                for key, item in list(value.items())[:_MAX_COLLECTION_ITEMS]
            }
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return cls._sanitize_string(str(value), root)

    @classmethod
    def _bound_result(cls, value: Any, root: Path) -> tuple[Any, bool]:
        sanitized = cls._sanitize(value, root)
        encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= FDX_MAX_RESPONSE_BYTES:
            return sanitized, False
        preview = encoded[:FDX_MAX_RESPONSE_BYTES].decode("utf-8", errors="ignore")
        return {
            "preview": preview,
            "note": "Structured FDX output exceeded CPTR_FDX_MAX_RESPONSE_BYTES and was truncated.",
        }, True

    async def execute(
        self,
        *,
        user_id: str,
        workspace_id: str,
        root: Path,
        identity: ExecutionIdentity,
        action: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        if action not in FDX_GATEWAY_ACTIONS:
            raise FdxIntelligenceError(
                "FDX_UNSUPPORTED_ACTION",
                f"unsupported FDX intelligence action: {action}",
                retriable=False,
            )
        if action in _REPOSITORY_BOUND_ACTIONS and not (root / ".git").exists():
            return {
                "workspace_id": workspace_id,
                "action": action,
                "provider": "fdx_native",
                "status": "degraded",
                "fallback_recommended": True,
                "error_code": "FDX_REPOSITORY_ROOT_REQUIRED",
                "reason": (
                    "The selected repo_path is not a Git repository root. Choose an authorized nested "
                    "repo_path or use normal CPTR Direct Coding tools. FDX will not walk above the "
                    "authorized root."
                ),
                "fallback_tools": [
                    "cptr_workspace_tree",
                    "cptr_workspace_search_symbols",
                    "cptr_code_search_files",
                    "cptr_code_read_file",
                    "cptr_code_get_git_status",
                ],
            }
        base = self._validate_ref(options.get("base"), "base")
        head = self._validate_ref(options.get("head"), "head")
        if base is not None:
            options["base"] = base
        if head is not None:
            options["head"] = head

        try:
            if action == "status":
                daemon = await self._daemon(
                    user_id=user_id, workspace_id=workspace_id, root=root, identity=identity
                )
                version_value = await daemon.request("version", {})
                version_text = (
                    str(version_value.get("version") or "")
                    if isinstance(version_value, dict)
                    else str(version_value or "")
                )
                negotiated = await daemon.request("health", {})
                capabilities = await daemon.negotiate()
                data: Any = {
                    "binary": Path(daemon.binary).name,
                    "version": {"text": f"fdx {version_text}" if version_text else "fdx unknown"},
                    "health": negotiated,
                    "daemon": capabilities,
                    "gateway_actions": list(FDX_GATEWAY_ACTIONS),
                }
            elif action in _DAEMON_ACTION_TO_OP:
                daemon = await self._daemon(
                    user_id=user_id, workspace_id=workspace_id, root=root, identity=identity
                )
                data = await daemon.request(
                    _DAEMON_ACTION_TO_OP[action], self._daemon_args(action, options)
                )
                # Older FDX daemons exposed a text-only `read` response and ignored
                # mode/symbol/dependency options. Preserve rolling-upgrade compatibility:
                # use the resident fast path only for the structured read contract.
                if action == "read" and not (
                    isinstance(data, dict) and isinstance(data.get("mode"), str)
                ):
                    data = await self._run_cli(
                        root=root,
                        identity=identity,
                        argv=self._cli_argv(action, options),
                    )
            else:
                data = await self._run_cli(
                    root=root,
                    identity=identity,
                    argv=self._cli_argv(action, options),
                )
            assurance = None
            if isinstance(data, dict):
                raw_assurance = data.get("assurance") or data.get("assurance_level")
                if raw_assurance is not None:
                    assurance = str(raw_assurance)
            degraded_evidence = bool(
                assurance and assurance.upper() in {"DEGRADED", "UNVERIFIED"}
            )
            if action == "semantic_references" and isinstance(data, dict):
                semantic_text = data.get("text")
                if isinstance(semantic_text, str) and "degraded=true" in semantic_text.lower():
                    degraded_evidence = True
            bounded, truncated = self._bound_result(data, root)
            degraded_evidence = degraded_evidence or truncated
            response: dict[str, Any] = {
                "workspace_id": workspace_id,
                "action": action,
                "provider": "fdx_native",
                "status": "degraded" if degraded_evidence else "ok",
                "fallback_recommended": degraded_evidence,
                "truncated": truncated,
                **({"assurance": assurance} if assurance else {}),
                "data": bounded,
            }
            if degraded_evidence:
                response["reason"] = (
                    "FDX returned degraded, unverified, or truncated intelligence; "
                    "corroborate with normal CPTR Direct Coding tools."
                )
                response["fallback_tools"] = [
                    "cptr_workspace_tree",
                    "cptr_workspace_search_symbols",
                    "cptr_code_search_files",
                    "cptr_code_read_file",
                    "cptr_code_get_git_status",
                ]
            return response
        except FdxUnavailable as exc:
            return {
                "workspace_id": workspace_id,
                "action": action,
                "provider": "fdx_native",
                "status": "unavailable",
                "fallback_recommended": True,
                "error_code": exc.code,
                "reason": self._sanitize_string(str(exc), root),
                "fallback_tools": [
                    "cptr_workspace_tree",
                    "cptr_workspace_search_symbols",
                    "cptr_code_search_files",
                    "cptr_code_read_file",
                    "cptr_code_get_git_status",
                ],
            }
        except FdxIntelligenceError as exc:
            return {
                "workspace_id": workspace_id,
                "action": action,
                "provider": "fdx_native",
                "status": "degraded",
                "fallback_recommended": True,
                "error_code": exc.code,
                "reason": self._sanitize_string(str(exc), root),
                "retriable": exc.retriable,
                "fallback_tools": [
                    "cptr_workspace_tree",
                    "cptr_workspace_search_symbols",
                    "cptr_code_search_files",
                    "cptr_code_read_file",
                    "cptr_code_get_git_status",
                ],
            }


service = FdxIntelligenceService()
