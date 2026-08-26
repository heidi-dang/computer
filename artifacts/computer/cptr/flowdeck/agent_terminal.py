"""Persistent, bounded PTY execution for the FlowDeck agent terminal.

This is intentionally a tool-side runtime, not a second chat transport.  A
session is owned by one authenticated FlowDeck run and emits safe observer
frames through the native CPTR event hook.  Raw terminal bytes are never
written to durable FlowDeck evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import FlowDeckMode
from cptr.flowdeck.durable import RunStatus
from cptr.flowdeck.terminal_observer import redact_terminal_text
from cptr.utils.identity import identity_for_context
from cptr.utils.terminal import TerminalSession, manager

MAX_COMMAND_CHARS = 12_000
MAX_RESULT_CHARS = 24_000
DEFAULT_TIMEOUT_SECONDS = 120.0
_PROTOCOL_OUTPUT = re.compile(r"__CPTR_AGENT_(?:READY|DONE)_[0-9a-f]+__(?:[0-9]+__)?")

_DANGEROUS_COMMANDS = (
    re.compile(r"(^|[;&|])\s*(?:sudo\s+)?(?:shutdown|reboot|poweroff)\b", re.I),
    re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b", re.I),
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:", re.I),
    re.compile(r"\brm\s+-[^\n;]*(?:/|~)(?:\s|$)", re.I),
    re.compile(r"(^|[;&|])\s*(?:exit|exec)\b", re.I),
)


class AgentTerminalPolicyError(RuntimeError):
    """Raised when server-side agent terminal gates reject a request."""


@dataclass
class _OwnedSession:
    run_id: str
    user_id: str
    workspace: Path
    session: TerminalSession
    observer: Any
    store: Any = None
    attempt_id: str | None = None
    buffer: bytearray = field(default_factory=bytearray)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader_task: asyncio.Task | None = None
    ready: bool = False
    closed: bool = False


_sessions: dict[tuple[str, str], _OwnedSession] = {}
_sessions_lock = asyncio.Lock()


def _bounded(text: str, limit: int = MAX_RESULT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n[output truncated]", True


def _display_output(chunk: bytes) -> str:
    return _PROTOCOL_OUTPUT.sub("", chunk.decode("utf-8", errors="replace"))


def _workspace_root(raw: Any) -> Path:
    root = Path(str(raw or "")).expanduser().resolve()
    if not root.is_dir():
        raise AgentTerminalPolicyError("owned workspace is not a directory")
    return root


def _safe_cwd(root: Path, raw: Any) -> Path:
    candidate = Path(str(raw or ".")).expanduser()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AgentTerminalPolicyError("terminal cwd must remain inside the owned workspace") from exc
    if not resolved.is_dir():
        raise AgentTerminalPolicyError("terminal cwd is not a directory")
    return resolved


def _validate_command(command: Any, config: FlowDeckConfig) -> str:
    value = str(command or "").strip()
    if not value:
        raise AgentTerminalPolicyError("command must not be empty")
    if len(value) > min(MAX_COMMAND_CHARS, config.max_terminal_command_chars):
        raise AgentTerminalPolicyError("command exceeds the server command limit")
    if "\x00" in value:
        raise AgentTerminalPolicyError("command contains an invalid NUL byte")
    if any(pattern.search(value) for pattern in _DANGEROUS_COMMANDS):
        raise AgentTerminalPolicyError("command rejected by the server safety policy")
    return value


async def _run_allows_terminal(context: dict[str, Any]) -> bool:
    """Keep terminal calls fenced to the active authenticated FlowDeck run."""
    store = context.get("flowdeck_store")
    if store is None:
        # Direct unit callers do not have durable lifecycle state. The coding
        # execution path always supplies the store and therefore gets the
        # stronger finalized-run fence below.
        return True
    run_id = str(context.get("flowdeck_run_id") or "")
    user_id = str(context.get("user_id") or "")
    if not run_id or not user_id:
        return False
    try:
        run = await store.get_run(run_id)
    except Exception:
        return False
    return bool(
        run
        and run.owner == user_id
        and run.status in {RunStatus.RUNNING.value, RunStatus.RECOVERING.value}
    )


async def _durable_event(owned: _OwnedSession, kind: str, payload: dict[str, Any]) -> None:
    if owned.store is None:
        return
    try:
        # Lifecycle metadata only; output text is deliberately excluded.
        safe_payload = {
            key: (
                redact_terminal_text(value, limit=4000)
                if key in {"command", "cwd"}
                else value
            )
            for key, value in payload.items()
            if key not in {"text", "output"}
        }
        await owned.store.record_event(
            owned.run_id,
            kind,
            {
                "user_id": owned.user_id,
                "session_id": owned.session.session_id,
                "attempt_id": owned.attempt_id,
                **safe_payload,
            },
        )
    except Exception:
        # Socket/observer delivery and command execution must not be turned
        # into a false success by a non-authoritative audit side effect.
        return


async def _command_interrupted(
    owned: _OwnedSession,
    command: str,
    status: str,
) -> None:
    """Publish a safe interruption frame before discarding the command PTY."""
    try:
        await owned.observer(
            "command_exit",
            {
                "tool_name": "agent_terminal_command",
                "command": command,
                "status": status,
                "session_id": owned.session.session_id,
                "terminal_id": owned.session.session_id,
                "attempt_id": owned.attempt_id,
                "session_discarded": True,
                "next_command_starts_fresh": True,
            },
        )
    except Exception:
        # Interruption cleanup must still discard the PTY if realtime delivery
        # is unavailable. The durable lifecycle record is best effort too.
        pass
    await _durable_event(
        owned,
        (
            "AGENT_TERMINAL_COMMAND_TIMED_OUT"
            if status == "timed_out"
            else "AGENT_TERMINAL_COMMAND_CANCELLED"
        ),
        {
            "command": command,
            "status": status,
            "session_discarded": True,
            "next_command_starts_fresh": True,
        },
    )


async def _read_loop(owned: _OwnedSession) -> None:
    try:
        while not owned.closed:
            chunk = await asyncio.to_thread(owned.session.read, 4096)
            if chunk:
                owned.buffer.extend(chunk)
                if len(owned.buffer) > MAX_RESULT_CHARS * 2:
                    del owned.buffer[: len(owned.buffer) - MAX_RESULT_CHARS * 2]
                owned.changed.set()
                if owned.ready:
                    await owned.observer(
                        "command_output",
                        {
                            "stream": "stdout",
                            "text": _display_output(chunk),
                            "session_id": owned.session.session_id,
                            "terminal_id": owned.session.session_id,
                            "attempt_id": owned.attempt_id,
                        },
                    )
            else:
                if not owned.session.is_alive():
                    owned.changed.set()
                    return
                await asyncio.sleep(0.02)
    except asyncio.CancelledError:
        raise
    except Exception:
        owned.changed.set()


async def _wait_for_marker(owned: _OwnedSession, marker: bytes, timeout: float) -> bytes:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        position = bytes(owned.buffer).find(marker)
        if position >= 0:
            before = bytes(owned.buffer[:position])
            del owned.buffer[: position + len(marker)]
            return before
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        owned.changed.clear()
        try:
            await asyncio.wait_for(owned.changed.wait(), min(remaining, 0.25))
        except asyncio.TimeoutError:
            continue


async def _wait_for_completion(
    owned: _OwnedSession, marker: bytes, timeout: float
) -> tuple[bytes, int]:
    pattern = re.compile(re.escape(marker) + rb"([0-9]+)__")
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        match = pattern.search(bytes(owned.buffer))
        if match:
            before = bytes(owned.buffer[: match.start()])
            del owned.buffer[: match.end()]
            return before, int(match.group(1))
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        owned.changed.clear()
        try:
            await asyncio.wait_for(owned.changed.wait(), min(remaining, 0.25))
        except asyncio.TimeoutError:
            continue


async def _new_session(
    *,
    run_id: str,
    user_id: str,
    workspace: Path,
    context: dict[str, Any],
) -> _OwnedSession:
    identity = await identity_for_context({"request": context.get("request"), "user_id": user_id})
    terminal = await asyncio.to_thread(manager.create, identity, cwd=str(workspace))
    owned = _OwnedSession(
        run_id=run_id,
        user_id=user_id,
        workspace=workspace,
        session=terminal,
        observer=context["terminal_observer"],
        store=context.get("flowdeck_store"),
        attempt_id=context.get("flowdeck_attempt_id"),
    )
    owned.reader_task = asyncio.create_task(_read_loop(owned))
    ready = uuid.uuid4().hex
    # TerminalSession starts an interactive shell; wait for its rcfile and
    # controlling-terminal setup before sending the protocol handshake.
    await asyncio.sleep(0.1)
    await asyncio.to_thread(
        terminal.write,
        (
            "stty -echo 2>/dev/null; "
            "PS1=''; PROMPT_COMMAND=''; export PS1 PROMPT_COMMAND; "
            f"printf '\\n__CPTR_AGENT_READY_{ready}__\\n'\n"
        ).encode(),
    )
    try:
        await _wait_for_marker(owned, f"__CPTR_AGENT_READY_{ready}__".encode(), 5)
    except BaseException:
        await _close_owned((user_id, run_id), owned)
        raise
    owned.buffer.clear()
    # Interactive shells may write the prompt immediately after the ready
    # marker. Give that write a bounded drain window before the first command.
    await asyncio.sleep(0.05)
    owned.buffer.clear()
    owned.ready = True
    await _durable_event(
        owned,
        "AGENT_TERMINAL_SESSION_STARTED",
        {"cwd": str(workspace), "status": "ready"},
    )
    return owned


async def _get_session(context: dict[str, Any], cwd: Any) -> _OwnedSession:
    run_id = str(context.get("flowdeck_run_id") or "")
    user_id = str(context.get("user_id") or "")
    if not run_id or not user_id:
        raise AgentTerminalPolicyError("FlowDeck run identity is required")
    root = _workspace_root(context.get("workspace"))
    requested_cwd = _safe_cwd(root, cwd)
    key = (user_id, run_id)
    async with _sessions_lock:
        owned = _sessions.get(key)
        if owned and (owned.closed or not owned.session.is_alive()):
            await _close_owned(key, owned)
            owned = None
        if owned is None:
            owned = await _new_session(
                run_id=run_id,
                user_id=user_id,
                workspace=requested_cwd,
                context=context,
            )
            _sessions[key] = owned
        elif requested_cwd != owned.workspace:
            # Cwd changes are performed in the persistent shell, not by
            # replacing the process. This input is only the initial cwd.
            pass
        return owned


async def _close_owned(key: tuple[str, str], owned: _OwnedSession) -> None:
    owned.closed = True
    if owned.reader_task:
        owned.reader_task.cancel()
        with suppress(asyncio.CancelledError):
            await owned.reader_task
    try:
        if os.name != "nt" and owned.session._pid > 0:
            os.killpg(owned.session._pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    # manager.create registers the PTY in CPTR's global user-terminal registry.
    # Remove it there as well as closing the process so agent sessions cannot
    # leak into the direct user terminal list.
    if not manager.close(None, owned.session.session_id):
        owned.session.close()
    if _sessions.get(key) is owned:
        _sessions.pop(key, None)


async def execute_agent_terminal_command(
    command: str,
    cwd: str = ".",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    __context__: dict[str, Any],
) -> str:
    """Run a bounded command in Heidi's persistent FlowDeck PTY.

    :param command: Shell command to execute in the persistent agent session.
    :param cwd: Initial workspace-relative cwd; later commands preserve shell cwd.
    :param timeout_seconds: Maximum command duration before the PTY is discarded.
    """
    config = FlowDeckConfig.from_env()
    if not (
        config.agent_terminal_enabled
        and config.enabled
        and config.mode in {FlowDeckMode.CONTROLLED, FlowDeckMode.FULL}
        and config.governance == "strict"
        and config.mutating_agents
        and not config.global_kill_switch
    ):
        return "Error: persistent agent terminal is disabled by server policy."
    try:
        value = _validate_command(command, config)
    except AgentTerminalPolicyError as exc:
        return f"Error: {exc}"
    try:
        timeout = max(1.0, min(float(timeout_seconds), config.max_terminal_timeout_seconds))
    except (TypeError, ValueError):
        return "Error: timeout_seconds must be a number."
    key = (str(__context__.get("user_id") or ""), str(__context__.get("flowdeck_run_id") or ""))
    if not await _run_allows_terminal(__context__):
        return "Error: FlowDeck run is no longer active; terminal session is closed."
    try:
        owned = await _get_session(__context__, cwd)
    except AgentTerminalPolicyError as exc:
        return f"Error: {exc}"
    marker = f"__CPTR_AGENT_DONE_{uuid.uuid4().hex}__".encode()
    async with owned.command_lock:
        try:
            await owned.observer(
                "command_start",
                {
                    "tool_name": "agent_terminal_command",
                    "command": value,
                    "cwd": str(owned.workspace),
                    "session_id": owned.session.session_id,
                    "terminal_id": owned.session.session_id,
                    "attempt_id": owned.attempt_id,
                },
            )
            await _durable_event(
                owned,
                "AGENT_TERMINAL_COMMAND_STARTED",
                {"command": value, "cwd": str(owned.workspace), "status": "running"},
            )
            wrapped = (
                f"{value}\n"
                "__cptr_status=$?\n"
                f"printf '\\n{marker.decode()}%s__\\n' \"$__cptr_status\"\n"
            )
            await asyncio.to_thread(owned.session.write, wrapped.encode())
            raw, exit_code = await _wait_for_completion(owned, marker, timeout)
            output = redact_terminal_text(
                raw.decode("utf-8", errors="replace"),
                limit=config.max_terminal_output_chars,
            )
            output, truncated = _bounded(output, config.max_terminal_output_chars)
            result = {
                "status": "succeeded" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "output": output,
                "truncated": truncated,
                "session_id": owned.session.session_id,
                "cwd": str(owned.workspace),
            }
            await owned.observer(
                "command_exit",
                {
                    "tool_name": "agent_terminal_command",
                    "command": value,
                    "status": result["status"],
                    "exit_code": exit_code,
                    "session_id": owned.session.session_id,
                    "terminal_id": owned.session.session_id,
                    "attempt_id": owned.attempt_id,
                },
            )
            await _durable_event(
                owned,
                "AGENT_TERMINAL_COMMAND_FINISHED",
                {
                    "command": value,
                    "status": result["status"],
                    "exit_code": exit_code,
                    "truncated": truncated,
                },
            )
            return json.dumps(result, ensure_ascii=False)
        except asyncio.TimeoutError:
            await _command_interrupted(owned, value, "timed_out")
            await _close_owned(key, owned)
            return json.dumps(
                {
                    "status": "timed_out",
                    "output": "",
                    "truncated": False,
                    "session_id": owned.session.session_id,
                    "session_discarded": True,
                    "next_command_starts_fresh": True,
                    "retryable": True,
                }
            )
        except asyncio.CancelledError:
            await _command_interrupted(owned, value, "cancelled")
            await _close_owned(key, owned)
            raise
        except (BrokenPipeError, OSError) as exc:
            await _close_owned(key, owned)
            return f"Error: persistent terminal stopped: {exc}"


async def close_agent_terminal(run_id: str, user_id: str) -> None:
    """Close one run-owned PTY during cancellation or process shutdown."""
    key = (user_id, run_id)
    async with _sessions_lock:
        owned = _sessions.get(key)
        if owned:
            await _close_owned(key, owned)