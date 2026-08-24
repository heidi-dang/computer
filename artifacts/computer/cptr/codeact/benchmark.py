"""Deterministic harness for same-model native-tool vs CodeAct comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from cptr.codeact.contracts import CodeActMode
from cptr.codeact.telemetry import ExecutionTelemetry


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    prompt: str
    expected: Any


@dataclass(frozen=True)
class BenchmarkObservation:
    case: str
    mode: CodeActMode
    telemetry: dict
    result: Any


Runner = Callable[[BenchmarkCase, ExecutionTelemetry], Awaitable[Any]]


async def run_same_model_ab(
    cases: list[BenchmarkCase],
    *,
    native_runner: Runner,
    codeact_runner: Runner,
) -> list[BenchmarkObservation]:
    """Run paired cases in stable order; runners supply model/token counters."""
    observations: list[BenchmarkObservation] = []
    for case in cases:
        for mode, runner in (
            (CodeActMode.DISABLED, native_runner),
            (CodeActMode.READ_ONLY, codeact_runner),
        ):
            telemetry = ExecutionTelemetry(mode=mode)
            telemetry.start()
            result = await runner(case, telemetry)
            telemetry.correctness = result == case.expected
            telemetry.finish()
            observations.append(
                BenchmarkObservation(
                    case=case.name,
                    mode=mode,
                    telemetry=telemetry.as_dict(),
                    result=result,
                )
            )
    return observations
