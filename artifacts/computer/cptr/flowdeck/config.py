"""Environment-only FlowDeck configuration.

Reading configuration is deliberately cheap and has no application side
effects. Database, model, process, and CPTR lifecycle initialization do not
belong here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cptr.flowdeck.contracts import FlowDeckMode


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FlowDeckConfig:
    enabled: bool = False
    coordinator_enabled: bool = False
    mode: FlowDeckMode = FlowDeckMode.OFF
    governance: str = "strict"
    max_specialists: int = 4
    max_delegation_depth: int = 1
    max_diagnostic_chars: int = 2000
    mutating_agents: bool = False
    coding_role: str = "backend-coder"
    max_steps: int = 20
    max_attempts: int = 40
    max_tool_calls: int = 200
    max_model_turns: int = 100
    max_wall_seconds: int = 1800
    global_kill_switch: bool = False
    disabled_specialists: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> FlowDeckConfig:
        """Read FlowDeck settings without initializing any runtime service."""
        enabled = _bool(
            os.environ.get("CPTR_FLOWDECK_ENABLED", os.environ.get("FLOWDECK_ENABLED")),
            False,
        )
        if not enabled:
            # Do not parse or initialize anything beyond the disabled fast path.
            return cls()

        raw_mode = os.environ.get("CPTR_FLOWDECK_MODE", os.environ.get("FLOWDECK_MODE", "shadow"))
        try:
            mode = FlowDeckMode(raw_mode.strip().lower())
        except ValueError as exc:
            raise ValueError(f"invalid FlowDeck mode: {raw_mode!r}") from exc

        governance = os.environ.get(
            "CPTR_FLOWDECK_GOVERNANCE", os.environ.get("FLOWDECK_GOVERNANCE", "strict")
        ).strip().lower()
        if governance not in {"strict", "permissive"}:
            raise ValueError(f"invalid FlowDeck governance mode: {governance!r}")

        max_specialists = _positive_int("CPTR_FLOWDECK_MAX_SPECIALISTS", 4)
        max_depth = _nonnegative_int("CPTR_FLOWDECK_MAX_DELEGATION_DEPTH", 1)
        max_diagnostic_chars = _positive_int("CPTR_FLOWDECK_MAX_DIAGNOSTIC_CHARS", 2000)
        mutating_agents = _bool(os.environ.get("CPTR_FLOWDECK_MUTATING_AGENTS"), False)
        coordinator_enabled = _bool(
            os.environ.get("CPTR_FLOWDECK_COORDINATOR_ENABLED"), False
        )
        coding_role = os.environ.get("CPTR_FLOWDECK_CODING_ROLE", "backend-coder").strip()
        global_kill_switch = _bool(os.environ.get("CPTR_FLOWDECK_KILL_SWITCH"), False)
        disabled_specialists = frozenset(
            item.strip()
            for item in os.environ.get("CPTR_FLOWDECK_DISABLED_SPECIALISTS", "").split(",")
            if item.strip()
        )
        return cls(
            enabled=True,
            coordinator_enabled=coordinator_enabled,
            mode=mode,
            governance=governance,
            max_specialists=max_specialists,
            max_delegation_depth=max_depth,
            max_diagnostic_chars=max_diagnostic_chars,
            mutating_agents=mutating_agents,
            coding_role=coding_role,
            max_steps=_positive_int("CPTR_FLOWDECK_MAX_STEPS", 20),
            max_attempts=_positive_int("CPTR_FLOWDECK_MAX_ATTEMPTS", 40),
            max_tool_calls=_positive_int("CPTR_FLOWDECK_MAX_TOOL_CALLS", 200),
            max_model_turns=_positive_int("CPTR_FLOWDECK_MAX_MODEL_TURNS", 100),
            max_wall_seconds=_positive_int("CPTR_FLOWDECK_MAX_WALL_SECONDS", 1800),
            global_kill_switch=global_kill_switch,
            disabled_specialists=disabled_specialists,
        )


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _nonnegative_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value