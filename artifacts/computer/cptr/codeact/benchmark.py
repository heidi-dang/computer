"""Same-model native-tool vs CodeAct qualification benchmark.

The provider is injected deliberately: CPTR's existing model resolver and
request client remain the sole provider boundary, while this module owns
pairing, measurement, adversarial coverage, scoring, and reporting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from cptr.codeact.contracts import CodeActMode
from cptr.codeact.sandbox import CodeActSandboxError, validate_program
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


@dataclass(frozen=True)
class ProviderMeasurement:
    """Normalized provider response used by both benchmark arms."""

    result: Any
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cycles: int = 1
    capability_calls: int = 0
    context_bytes: int = 0


@dataclass(frozen=True)
class SecurityCase:
    name: str
    category: str
    program: str


@dataclass(frozen=True)
class SecurityObservation:
    name: str
    category: str
    blocked: bool
    error: str = ""


@dataclass(frozen=True)
class BenchmarkReport:
    model_id: str
    observations: tuple[BenchmarkObservation, ...]
    security: tuple[SecurityObservation, ...]
    score: float
    decision: str
    provider_backed: bool
    limitations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider_backed": self.provider_backed,
            "observations": [
                {
                    "case": item.case,
                    "mode": item.mode.value,
                    "telemetry": item.telemetry,
                    "result": item.result,
                }
                for item in self.observations
            ],
            "security": [item.__dict__ for item in self.security],
            "score": self.score,
            "decision": self.decision,
            "limitations": list(self.limitations),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, default=str)


SECURITY_CASES: tuple[SecurityCase, ...] = (
    SecurityCase("import-os", "import", "import os"),
    SecurityCase("introspection-class", "introspection", "print((1).__class__)"),
    SecurityCase("filesystem-open", "filesystem", "open('/etc/passwd').read()"),
    SecurityCase("environment-read", "environment", "print(__import__('os').environ)"),
    SecurityCase("socket-network", "socket", "import socket\nsocket.socket()"),
    SecurityCase("subprocess", "subprocess", "import subprocess\nsubprocess.run(['id'])"),
    SecurityCase("serialization-pickle", "serialization", "import pickle\npickle.loads(b'c builtins\\nprint\\n.')"),
)


Runner = Callable[[BenchmarkCase, ExecutionTelemetry], Awaitable[Any]]
ProviderRunner = Callable[
    [BenchmarkCase, CodeActMode, ExecutionTelemetry], Awaitable[ProviderMeasurement]
]


async def run_same_model_ab(
    cases: list[BenchmarkCase],
    *,
    native_runner: Runner,
    codeact_runner: Runner,
    model_id: str = "",
) -> list[BenchmarkObservation]:
    """Run paired cases in stable order; runners supply model/token counters."""
    observations: list[BenchmarkObservation] = []
    for case in cases:
        for mode, runner in (
            (CodeActMode.DISABLED, native_runner),
            (CodeActMode.READ_ONLY, codeact_runner),
        ):
            telemetry = ExecutionTelemetry(mode=mode, model_id=model_id)
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


async def run_provider_benchmark(
    cases: Iterable[BenchmarkCase],
    *,
    model_id: str,
    provider_runner: ProviderRunner,
    provider_backed: bool = False,
    security_cases: Iterable[SecurityCase] = SECURITY_CASES,
    security_validator: Callable[[str], Any] = validate_program,
) -> BenchmarkReport:
    """Run paired provider-backed arms and the complete sandbox escape corpus.

    ``provider_runner`` must resolve the same CPTR model for both modes. It is
    intentionally passed the telemetry object so the native and CodeAct
    adapters can report provider usage without this module knowing provider
    response schemas.
    """
    if not model_id.strip():
        raise ValueError("model_id must not be blank")
    observations: list[BenchmarkObservation] = []
    cases = tuple(cases)
    for case in cases:
        for mode in (CodeActMode.DISABLED, CodeActMode.READ_ONLY):
            telemetry = ExecutionTelemetry(mode=mode, model_id=model_id)
            telemetry.start()
            measurement = await provider_runner(case, mode, telemetry)
            telemetry.input_tokens = measurement.input_tokens
            telemetry.output_tokens = measurement.output_tokens
            telemetry.total_tokens = measurement.total_tokens or (
                measurement.input_tokens + measurement.output_tokens
            )
            telemetry.cycles = measurement.cycles
            telemetry.model_invocations = measurement.cycles
            telemetry.capability_calls = measurement.capability_calls
            telemetry.context_bytes = measurement.context_bytes
            telemetry.context_result_bytes = measurement.context_bytes
            telemetry.correctness = measurement.result == case.expected
            telemetry.finish()
            observations.append(
                BenchmarkObservation(
                    case=case.name,
                    mode=mode,
                    telemetry=telemetry.as_dict(),
                    result=measurement.result,
                )
            )

    security: list[SecurityObservation] = []
    for case in security_cases:
        try:
            security_validator(case.program)
        except CodeActSandboxError as exc:
            security.append(SecurityObservation(case.name, case.category, True, str(exc)))
        except Exception as exc:  # A validator failure is not a passing block.
            security.append(SecurityObservation(case.name, case.category, False, str(exc)))
        else:
            security.append(SecurityObservation(case.name, case.category, False, "accepted"))

    correctness = sum(bool(item.telemetry["correctness"]) for item in observations)
    correctness_rate = correctness / len(observations) if observations else 0.0
    security_rate = sum(item.blocked for item in security) / len(security) if security else 0.0
    score = round((correctness_rate * 0.6 + security_rate * 0.4) * 100, 2)
    decision = (
        "enable-read-only"
        if provider_backed and observations and correctness_rate == 1 and security_rate == 1
        else "keep-disabled"
    )
    limitations: tuple[str, ...] = ()
    if not provider_backed:
        limitations += ("Provider-backed execution was not confirmed; fixture results cannot qualify enablement.",)
    if not observations:
        limitations += ("No task corpus was supplied; no performance qualification is possible.",)
    return BenchmarkReport(
        model_id=model_id,
        observations=tuple(observations),
        security=tuple(security),
        score=score,
        decision=decision,
        provider_backed=provider_backed,
        limitations=limitations,
    )
