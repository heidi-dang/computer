"""In-process execution-plane state boundary for command sessions.

The API layer deliberately talks to this registry instead of owning ad-hoc
lifecycle policy. It remains in-process today because CPTR command/process and
browser ownership are process-local; this boundary is the seam for a future IPC
execution service without prematurely enabling unsafe multi-worker serving.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from cptr.env import COMMAND_SESSION_MAX_RETAINED, COMMAND_SESSION_TTL_SECONDS


class CommandSessionRegistry:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.total_created = 0
        self.total_reaped = 0

    def register(self, session_id: str, session: dict[str, Any]) -> None:
        self.sessions[session_id] = session
        self.total_created += 1

    def get(self, session_id: str) -> dict[str, Any] | None:
        return self.sessions.get(session_id)

    def remove(self, session_id: str) -> dict[str, Any] | None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            self.total_reaped += 1
        return session

    def values(self) -> Iterable[dict[str, Any]]:
        return self.sessions.values()

    def active_count(self, user_id: str | None = None) -> int:
        return sum(
            1
            for session in self.sessions.values()
            if not session.get("done") and (user_id is None or session.get("user_id") == user_id)
        )

    def reap(self, *, now: float | None = None) -> list[str]:
        """Evict expired completed sessions and enforce a hard retained cap."""
        current = time.time() if now is None else now
        removable = [
            (session_id, session)
            for session_id, session in self.sessions.items()
            if session.get("done")
            and current - float(session.get("completed_at") or session.get("created_at") or current)
            >= COMMAND_SESSION_TTL_SECONDS
        ]
        removed: list[str] = []
        for session_id, _ in removable:
            if self.remove(session_id) is not None:
                removed.append(session_id)

        completed = sorted(
            (
                (session_id, session)
                for session_id, session in self.sessions.items()
                if session.get("done")
            ),
            key=lambda item: float(item[1].get("completed_at") or item[1].get("created_at") or 0.0),
            reverse=True,
        )
        excess = max(0, len(completed) - COMMAND_SESSION_MAX_RETAINED)
        for session_id, _ in reversed(completed[-excess:] if excess else []):
            if self.remove(session_id) is not None:
                removed.append(session_id)
        return removed

    def stats(self) -> dict[str, int]:
        active = sum(1 for session in self.sessions.values() if not session.get("done"))
        completed = len(self.sessions) - active
        retained_output_bytes = sum(
            len(session.get("output") or b"") for session in self.sessions.values()
        )
        return {
            "active": active,
            "completed_retained": completed,
            "total_retained": len(self.sessions),
            "retained_output_bytes": retained_output_bytes,
            "total_created": self.total_created,
            "total_reaped": self.total_reaped,
            "retained_cap": COMMAND_SESSION_MAX_RETAINED,
        }


command_session_registry = CommandSessionRegistry()
