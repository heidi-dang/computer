"""Bounded execution-observer frames for Heidi's Live Terminal.

This module is deliberately transport-agnostic: callers provide CPTR's existing
authenticated event emitter. It never creates a socket or persists a second
transcript.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Any, Awaitable, Callable

_MAX_FRAMES_PER_RUN = 256
_MAX_TEXT = 4000
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(--?(?:api[-_]?key|token|password|secret)\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[-_]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+"),
)

_buffers: dict[str, deque[dict[str, Any]]] = defaultdict(
    lambda: deque(maxlen=_MAX_FRAMES_PER_RUN)
)
_sequences: dict[str, int] = defaultdict(int)
_lock = Lock()


def redact_terminal_text(value: Any) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text[:_MAX_TEXT]


def safe_terminal_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    safe: dict[str, Any] = {}
    for key in (
        "command",
        "cwd",
        "stream",
        "text",
        "exit_code",
        "status",
        "tool_name",
        "specialist_id",
        "child_agent_id",
        "attempt_id",
        "step_id",
        "session_id",
        "terminal_id",
    ):
        if key in payload and payload[key] is not None:
            safe[key] = (
                redact_terminal_text(payload[key])
                if key in {"command", "cwd", "text"}
                else payload[key]
            )
    return safe


async def emit_terminal_frame(
    *,
    user_id: str,
    emit: Callable[..., Awaitable[None]],
    kind: str,
    payload: dict[str, Any] | None = None,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    message_id: str | None = None,
    delivery_chat_id: str | None = None,
    delivery_message_id: str | None = None,
) -> dict[str, Any]:
    """Create, buffer, and send one safe observer frame over CPTR's event path."""
    owner = str(parent_run_id or run_id or message_id or f"user:{user_id}")
    with _lock:
        _sequences[owner] += 1
        frame = {
            "kind": "terminal_frame",
            "frame_kind": kind,
            "payload": safe_terminal_payload(payload),
            "sequence": _sequences[owner],
            "created_at": int(time.time() * 1000),
            "terminal_run_id": run_id,
            "terminal_parent_run_id": parent_run_id,
            "terminal_message_id": message_id,
        }
        _buffers[owner].append(frame)
    await emit(
        kind="terminal_frame",
        payload=frame,
        terminal_frame=True,
        sequence=frame["sequence"],
        created_at=frame["created_at"],
        **({"chat_id": delivery_chat_id} if delivery_chat_id else {}),
        **({"message_id": delivery_message_id} if delivery_message_id else {}),
    )
    return frame


def recent_terminal_frames(run_id: str, limit: int = _MAX_FRAMES_PER_RUN) -> list[dict[str, Any]]:
    """Return a bounded in-memory replay window for a reconnecting owner."""
    with _lock:
        return list(_buffers.get(run_id, ()))[: max(0, limit)]