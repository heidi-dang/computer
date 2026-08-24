"""Server-owned CodeAct contracts and feature controls."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class CodeActMode(StrEnum):
    DISABLED = "disabled"
    EVALUATION = "evaluation"
    READ_ONLY = "read_only"


QUALIFICATION_CASE_NAMES = frozenset({"release-label", "inventory-total", "ready-owner"})
QUALIFICATION_OBSERVATIONS = frozenset(
    (case_name, mode)
    for case_name in QUALIFICATION_CASE_NAMES
    for mode in (CodeActMode.DISABLED.value, CodeActMode.READ_ONLY.value)
)
QUALIFICATION_SECURITY_CASES = frozenset(
    {
        ("import-os", "import"),
        ("introspection-class", "introspection"),
        ("filesystem-open", "filesystem"),
        ("environment-read", "environment"),
        ("socket-network", "socket"),
        ("subprocess", "subprocess"),
        ("serialization-pickle", "serialization"),
    }
)


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class CodeActLimits:
    wall_seconds: float = 15.0
    cpu_seconds: int = 10
    memory_bytes: int = 256 * 1024 * 1024
    max_code_chars: int = 24_000
    max_output_chars: int = 16_000
    max_capability_calls: int = 64
    max_repair_cycles: int = 2


@dataclass(frozen=True)
class CodeActConfig:
    """Read-only, server-owned controls. All defaults fail closed."""

    mode: CodeActMode = CodeActMode.DISABLED
    allowed_roles: frozenset[str] = frozenset()
    kill_switch: bool = False
    qualification_report_path: str = ""
    limits: CodeActLimits = field(default_factory=CodeActLimits)

    @property
    def enabled(self) -> bool:
        return self.mode is not CodeActMode.DISABLED and not self.kill_switch

    def allows_role(self, role: str) -> bool:
        return self.enabled and role in self.allowed_roles

    def allows_qualified_model(self, model_id: str) -> bool:
        """Fail closed unless a complete provider-backed report approved this model."""
        if not model_id or not self.qualification_report_path:
            return False
        try:
            report = json.loads(Path(self.qualification_report_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        observations = report.get("observations")
        security = report.get("security")
        if not isinstance(observations, list) or not isinstance(security, list):
            return False
        try:
            observed_cases = {(item["case"], item["mode"]) for item in observations}
            observed_security = {(item["name"], item["category"]) for item in security}
        except (KeyError, TypeError):
            return False
        return bool(
            report.get("provider_backed") is True
            and report.get("model_id") == model_id
            and report.get("decision") == "enable-read-only"
            and report.get("score") == 100.0
            and observed_cases == QUALIFICATION_OBSERVATIONS
            and all(item.get("telemetry", {}).get("correctness") is True for item in observations)
            and observed_security == QUALIFICATION_SECURITY_CASES
            and all(item.get("blocked") is True for item in security)
        )

    @classmethod
    def from_env(cls) -> "CodeActConfig":
        raw = os.environ.get("CPTR_CODEACT_MODE", "disabled").strip().lower()
        try:
            mode = CodeActMode(raw)
        except ValueError as exc:
            raise ValueError(f"invalid CPTR_CODEACT_MODE: {raw!r}") from exc
        roles = frozenset(
            item.strip()
            for item in os.environ.get("CPTR_CODEACT_READ_ONLY_ROLES", "").split(",")
            if item.strip()
        )
        return cls(
            mode=mode,
            allowed_roles=roles,
            kill_switch=_bool("CPTR_CODEACT_KILL_SWITCH"),
            qualification_report_path=os.environ.get(
                "CPTR_CODEACT_QUALIFICATION_REPORT", ""
            ).strip(),
            limits=CodeActLimits(
                wall_seconds=float(os.environ.get("CPTR_CODEACT_WALL_SECONDS", "15")),
                cpu_seconds=_int("CPTR_CODEACT_CPU_SECONDS", 10),
                memory_bytes=_int("CPTR_CODEACT_MEMORY_BYTES", 256 * 1024 * 1024),
                max_code_chars=_int("CPTR_CODEACT_MAX_CODE_CHARS", 24_000),
                max_output_chars=_int("CPTR_CODEACT_MAX_OUTPUT_CHARS", 16_000),
                max_capability_calls=_int("CPTR_CODEACT_MAX_CAPABILITY_CALLS", 64),
                max_repair_cycles=_int("CPTR_CODEACT_MAX_REPAIR_CYCLES", 2, 0),
            ),
        )


@dataclass(frozen=True)
class CodeActIdentity:
    """All identity fields are created by CPTR, never by generated code."""

    user_id: str
    workspace: str
    task_id: str
    run_id: str | None = None
    step_id: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    model_id: str = ""
    repl_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class CapabilityCall:
    sequence: int
    name: str
    arguments: dict[str, Any]
    identity: CodeActIdentity
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class CodeActResult:
    output: str
    result: Any = None
    execution_id: str = ""
    capability_calls: tuple[CapabilityCall, ...] = ()
    elapsed_ms: int = 0
    output_truncated: bool = False
