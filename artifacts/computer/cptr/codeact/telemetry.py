"""Comparable CodeAct/tool-calling measurement records."""

from __future__ import annotations

import time
from dataclasses import dataclass

from cptr.codeact.contracts import CodeActMode


@dataclass
class ExecutionTelemetry:
    mode: CodeActMode
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cycles: int = 0
    model_invocations: int = 0
    capability_calls: int = 0
    context_bytes: int = 0
    context_result_bytes: int = 0
    correctness: bool | None = None
    started_at: float = 0.0
    ended_at: float = 0.0

    def start(self) -> None:
        self.started_at = time.monotonic()

    def finish(self) -> None:
        self.ended_at = time.monotonic()

    @property
    def latency_ms(self) -> int:
        end = self.ended_at or time.monotonic()
        return int(max(0.0, end - self.started_at) * 1000)

    def as_dict(self) -> dict[str, int | str | bool | None]:
        return {
            "mode": self.mode.value,
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cycles": self.cycles,
            "model_invocations": self.model_invocations,
            "capability_calls": self.capability_calls,
            "context_bytes": self.context_bytes,
            "context_result_bytes": self.context_result_bytes,
            "latency_ms": self.latency_ms,
            "correctness": self.correctness,
        }