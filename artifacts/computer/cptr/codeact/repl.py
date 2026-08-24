"""Persistent, per-attempt CodeAct REPL host.

The API process owns the session and capability authorization, while generated
Python runs in a short-lived restricted child process. The child can only ask
the host for explicitly supplied capability names over a private pipe.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cptr.codeact.contracts import (
    CapabilityCall,
    CodeActConfig,
    CodeActIdentity,
    CodeActResult,
)
from cptr.codeact.sandbox import CodeActSandboxError, validate_program

CapabilityHandler = Callable[..., Awaitable[Any]]


class CodeActCapabilityError(RuntimeError):
    """A capability was not in the server-authorized SDK or failed safely."""


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    handler: CapabilityHandler
    description: str


class ReadOnlyCapabilitySDK:
    """Narrow server-side capability registry; no service objects are exposed."""

    def __init__(self, capabilities: dict[str, CapabilitySpec]):
        self._capabilities = dict(capabilities)

    @classmethod
    def from_handlers(cls, handlers: dict[str, CapabilityHandler]) -> "ReadOnlyCapabilitySDK":
        descriptions = {
            "files.read": "Read bounded text from an owned workspace path.",
            "files.list": "List entries in an owned workspace directory.",
            "files.search": "Search files in an owned workspace.",
            "git.status": "Read the current status of the owned workspace repository.",
            "git.diff": "Read a bounded diff from the owned workspace repository.",
        }
        unknown = set(handlers) - set(descriptions)
        if unknown:
            raise CodeActCapabilityError(f"unknown CodeAct capability: {sorted(unknown)}")
        return cls(
            {
                name: CapabilitySpec(
                    name=name,
                    handler=handler,
                    description=descriptions[name],
                )
                for name, handler in handlers.items()
            }
        )

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._capabilities)

    @property
    def documentation(self) -> str:
        """Generate only documentation for capabilities actually authorized."""
        return "\n".join(
            f"- cptr.{name.replace('.', '.')}(**kwargs): {spec.description}"
            for name, spec in sorted(self._capabilities.items())
        )

    async def call(self, name: str, arguments: dict[str, Any], call: CapabilityCall) -> Any:
        spec = self._capabilities.get(name)
        if spec is None:
            raise CodeActCapabilityError(f"capability denied: {name}")
        try:
            return await spec.handler(**arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise CodeActCapabilityError(f"{name} failed: {type(exc).__name__}") from exc


class CodeActRepl:
    """One persistent interpreter process per CPTR physical attempt."""

    def __init__(
        self,
        *,
        identity: CodeActIdentity,
        sdk: ReadOnlyCapabilitySDK,
        config: CodeActConfig | None = None,
        process_factory: Callable[..., subprocess.Popen[str]] | None = None,
    ):
        self.identity = identity
        self.sdk = sdk
        self.config = config or CodeActConfig()
        self.session_id = identity.repl_session_id
        self._process_factory = process_factory or subprocess.Popen
        self._process: subprocess.Popen[str] | None = None
        self._calls: list[CapabilityCall] = []
        self._sequence = 0
        self._closed = False
        self._started_at = 0.0
        self._execute_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def capability_calls(self) -> tuple[CapabilityCall, ...]:
        return tuple(self._calls)

    def _start(self) -> None:
        if self._closed:
            raise RuntimeError("CodeAct REPL is closed")
        if self._process is not None:
            return
        worker = str(Path(__file__).with_name("worker.py"))
        env = {
            "PATH": os.environ.get("PATH", ""),
            "CPTR_CODEACT_MAX_CODE_CHARS": str(self.config.limits.max_code_chars),
            "CPTR_CODEACT_MAX_OUTPUT_CHARS": str(self.config.limits.max_output_chars),
            "CPTR_CODEACT_MAX_CAPABILITY_CALLS": str(self.config.limits.max_capability_calls),
        }

        def limit_resources():
            try:
                import resource

                resource.setrlimit(resource.RLIMIT_CPU, (self.config.limits.cpu_seconds, self.config.limits.cpu_seconds))
                # Python's launcher reserves a large virtual address arena on
                # this platform before importing the worker. Keep a bounded
                # ceiling, but do not make a valid worker unstartable.
                address_space = max(self.config.limits.memory_bytes, 1024 * 1024 * 1024)
                resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
            except (ImportError, OSError, ValueError):
                pass

        self._process = self._process_factory(
            [sys.executable, "-I", "-S", worker],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
            preexec_fn=limit_resources if os.name == "posix" else None,
        )
        self._started_at = time.monotonic()

    async def execute(self, code: str) -> CodeActResult:
        """Execute a block while retaining namespace state across calls."""
        # The worker protocol is request/response based; concurrent blocks on
        # one attempt must not interleave their responses.
        async with self._execute_lock:
            return await self._execute(code)

    async def _execute(self, code: str) -> CodeActResult:
        if not self.config.enabled:
            raise CodeActSandboxError("CodeAct is disabled by server policy")
        validate_program(code, max_chars=self.config.limits.max_code_chars)
        self._start()
        assert self._process and self._process.stdin and self._process.stdout
        execution_id = str(uuid.uuid4())
        started = time.monotonic()
        await asyncio.to_thread(self._write, {"type": "execute", "code": code})
        output = ""
        truncated = False
        while True:
            if time.monotonic() - started > self.config.limits.wall_seconds:
                await self.close(force=True)
                raise TimeoutError("CodeAct execution exceeded wall-clock limit")
            try:
                line = await asyncio.wait_for(
                    asyncio.to_thread(self._process.stdout.readline),
                    timeout=max(0.05, self.config.limits.wall_seconds - (time.monotonic() - started)),
                )
            except asyncio.TimeoutError:
                await self.close(force=True)
                raise TimeoutError("CodeAct execution exceeded wall-clock limit")
            if not line:
                await self.close(force=True)
                raise RuntimeError("CodeAct worker exited unexpectedly")
            try:
                message = json.loads(line)
            except (TypeError, json.JSONDecodeError) as exc:
                await self.close(force=True)
                raise CodeActSandboxError("CodeAct worker returned invalid protocol data") from exc
            if message.get("type") == "capability_call":
                self._sequence += 1
                call = CapabilityCall(
                    sequence=self._sequence,
                    name=str(message.get("name", "")),
                    arguments=dict(message.get("arguments") or {}),
                    identity=self.identity,
                )
                self._calls.append(call)
                try:
                    result = await self.sdk.call(call.name, call.arguments, call)
                    self._write({"ok": True, "result": result})
                except asyncio.CancelledError:
                    await self.close(force=True)
                    raise
                except Exception as exc:
                    try:
                        self._write({"ok": False, "error": str(exc)[:500]})
                    except (BrokenPipeError, OSError, RuntimeError):
                        await self.close(force=True)
                        raise
            elif message.get("type") == "result":
                output = str(message.get("output", ""))
                truncated = bool(message.get("truncated"))
                return CodeActResult(
                    output=output,
                    execution_id=execution_id,
                    capability_calls=tuple(self._calls),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    output_truncated=truncated,
                )
            elif message.get("type") == "error":
                await self.close(force=True)
                raise CodeActSandboxError(str(message.get("error", "CodeAct execution failed")))

    def _write(self, message: dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError("CodeAct worker is not running")
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    async def close(self, *, force: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        self._process = None
        if not process:
            return
        try:
            if not force and process.poll() is None and process.stdin:
                await asyncio.to_thread(self._write_to, process, {"type": "shutdown"})
                await asyncio.to_thread(process.wait, 1)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.poll() is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except (ProcessLookupError, OSError):
                    pass
            try:
                process.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                pass
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass

    @staticmethod
    def _write_to(process: subprocess.Popen[str], message: dict[str, Any]) -> None:
        if process.stdin:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

    async def __aenter__(self) -> "CodeActRepl":
        if not self.config.enabled:
            self._closed = True
            raise CodeActSandboxError("CodeAct is disabled by server policy")
        self._start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close(force=exc_type is not None)
