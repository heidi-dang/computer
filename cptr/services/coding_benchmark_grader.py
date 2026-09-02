"""Trusted coordinator for the standardized CPTR coding benchmark.

The coordinator never imports benchmark submissions.  Student source executes only
inside short-lived isolated Python child evaluators.  Children receive concrete
input scenarios, but never the grader seed, score weights, expected outputs, or
leaderboard state.  The parent process computes every expected result and every
point awarded.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
from collections import OrderedDict
from pathlib import Path

MAX_SCORE = 100

# This program is deliberately observation-only. It contains no expected values,
# score weights, seed, or pass/fail logic. The trusted parent scores observations.
_STUDENT_EVALUATOR = r"""import importlib.util
import json
import os
import sys
from pathlib import Path

# Capture the transport primitives before importing untrusted student source.
_json_loads = json.loads
_json_dumps = json.dumps
_os_write = os.write
_payload = _json_loads(sys.stdin.buffer.read().decode("utf-8"))
_task = str(_payload.get("task") or "")
_workspace = Path.cwd()


def _load(name):
    path = _workspace / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"cptr_benchmark_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _safe_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    return {"__type__": type(value).__name__, "__repr__": repr(value)[:160]}


def _error(exc):
    return {"kind": "exception", "type": type(exc).__name__, "message": str(exc)[:160]}


def _interval(module):
    merge = module.merge_intervals
    observations = []
    for raw in _payload.get("cases", []):
        sample = [tuple(item) for item in raw]
        original = [tuple(item) for item in sample]
        try:
            result = list(merge(sample))
            observations.append({
                "kind": "return",
                "value": _safe_value(result),
                "mutated": [tuple(item) for item in sample] != original,
            })
        except BaseException as exc:
            observations.append(_error(exc))
    return {"observations": observations}


def _ttl(module):
    Cache = module.TTLCache
    validations = []
    for invalid in _payload.get("validations", []):
        try:
            Cache(invalid[0], invalid[1])
            validations.append({"kind": "return"})
        except BaseException as exc:
            validations.append(_error(exc))

    scenarios = []
    for scenario in _payload.get("scenarios", []):
        observations = []
        try:
            cache = Cache(scenario["capacity"], scenario["ttl"])
        except BaseException as exc:
            scenarios.append({"constructor": _error(exc), "observations": []})
            continue
        for op in scenario.get("operations", []):
            try:
                if op["op"] == "set":
                    value = cache.set(op["key"], op["value"], op["now"])
                elif op["op"] == "get":
                    value = cache.get(op["key"], op["now"], op["default"])
                elif op["op"] == "delete":
                    value = cache.delete(op["key"])
                else:
                    raise ValueError("unknown operation")
                observations.append({"kind": "return", "value": _safe_value(value)})
            except BaseException as exc:
                observations.append(_error(exc))
        scenarios.append({"constructor": {"kind": "return"}, "observations": observations})
    return {"validations": validations, "scenarios": scenarios}


def _retry(module):
    retry_call = module.retry_call

    class Retryable(Exception):
        pass

    class Fatal(Exception):
        pass

    observations = []
    for scenario in _payload.get("scenarios", []):
        calls = {"n": 0}
        actions = list(scenario.get("actions", []))

        def fn():
            index = calls["n"]
            calls["n"] += 1
            action = actions[min(index, len(actions) - 1)] if actions else {"kind": "return", "value": None}
            if action["kind"] == "retryable":
                raise Retryable(str(action.get("message", "retry")))
            if action["kind"] == "fatal":
                raise Fatal(str(action.get("message", "fatal")))
            return action.get("value")

        try:
            value = retry_call(fn, attempts=scenario["attempts"], retryable=(Retryable,))
            observations.append({"kind": "return", "value": _safe_value(value), "calls": calls["n"]})
        except BaseException as exc:
            observations.append({**_error(exc), "calls": calls["n"]})
    return {"observations": observations}


try:
    if _task == "interval_merge":
        _result = _interval(_load("intervals"))
    elif _task == "ttl_lru_cache":
        _result = _ttl(_load("ttl_cache"))
    elif _task == "retry_policy":
        _result = _retry(_load("retry_policy"))
    else:
        raise ValueError("unknown task")
except BaseException as exc:
    _result = {"runner_error": {"type": type(exc).__name__, "message": str(exc)[:160]}}

# Write with a captured OS primitive so student reassignment of sys.stdout/json
# cannot manufacture scoring data. A closed/corrupt descriptor simply fails the
# run and earns no points in the trusted parent.
_blob = _json_dumps(_result, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
_os_write(1, _blob)
"""


class _RefCache:
    def __init__(self, capacity: int, ttl: float) -> None:
        self.capacity = capacity
        self.ttl = ttl
        self.items: OrderedDict[str, tuple[int, float]] = OrderedDict()

    def _purge(self, now: float) -> None:
        for key in [key for key, (_, expires) in self.items.items() if expires <= now]:
            self.items.pop(key, None)

    def set(self, key: str, value: int, now: float) -> None:
        self._purge(now)
        self.items.pop(key, None)
        self.items[key] = (value, now + self.ttl)
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)

    def get(self, key: str, now: float, default: str) -> int | str:
        self._purge(now)
        if key not in self.items:
            return default
        value, expires = self.items.pop(key)
        self.items[key] = (value, expires)
        return value

    def delete(self, key: str) -> bool:
        return self.items.pop(key, None) is not None


def _reference_merge(intervals: list[list[int]]) -> list[list[int]]:
    normalized = []
    for start, end in intervals:
        if start > end:
            start, end = end, start
        normalized.append([start, end])
    normalized.sort()
    output: list[list[int]] = []
    for start, end in normalized:
        if not output or start > output[-1][1]:
            output.append([start, end])
        else:
            output[-1][1] = max(output[-1][1], end)
    return output


def _score_case(
    case_id: str, weight: int, checks: list[tuple[bool, str | None]]
) -> dict[str, object]:
    passed = sum(1 for ok, _ in checks if ok)
    total = len(checks)
    points = round(weight * passed / total) if total else 0
    errors = sorted({kind for ok, kind in checks if not ok and kind})[:5]
    return {
        "id": case_id,
        "passed": passed,
        "total": total,
        "points": points,
        "max_points": weight,
        "error_kinds": errors,
    }


def _observation_check(
    observation: object, expected: object, *, require_unmutated: bool = False
) -> tuple[bool, str | None]:
    if not isinstance(observation, dict):
        return False, "protocol"
    if observation.get("kind") != "return":
        kind = observation.get("type")
        return False, str(kind or "exception")[:80]
    if require_unmutated and observation.get("mutated") is not False:
        return False, "mutation"
    return observation.get("value") == expected, None if observation.get(
        "value"
    ) == expected else "assertion"


async def _run_student(
    root: Path, payload: dict[str, object], *, timeout: float
) -> dict[str, object]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
    }
    process = await asyncio.create_subprocess_exec(
        os.sys.executable,
        "-I",
        "-c",
        _STUDENT_EVALUATOR,
        cwd=str(root),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    request = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(request), timeout=max(0.05, timeout)
        )
    except asyncio.TimeoutError as exc:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        await process.wait()
        raise TimeoutError("benchmark student evaluator timed out") from exc
    if process.returncode != 0:
        raise RuntimeError("benchmark student evaluator exited unsuccessfully")
    if len(stdout) > 1_000_000:
        raise RuntimeError("benchmark student evaluator returned oversized output")
    try:
        value = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("benchmark student evaluator returned invalid output") from exc
    if not isinstance(value, dict):
        raise RuntimeError("benchmark student evaluator returned invalid output")
    return value


async def _grade_interval(root: Path, rng: random.Random, timeout: float) -> dict[str, object]:
    cases: list[list[list[int]]] = [
        [],
        [[1, 2]],
        [[5, 1], [2, 3]],
        [[1, 3], [3, 6]],
        [[9, 10], [1, 2], [2, 4], [7, 8]],
    ]
    for _ in range(40):
        cases.append(
            [[rng.randint(-50, 50), rng.randint(-50, 50)] for _ in range(rng.randint(0, 18))]
        )
    result = await _run_student(root, {"task": "interval_merge", "cases": cases}, timeout=timeout)
    observations = (
        result.get("observations") if isinstance(result.get("observations"), list) else []
    )
    checks: list[tuple[bool, str | None]] = []
    for index, sample in enumerate(cases):
        observation = observations[index] if index < len(observations) else None
        checks.append(
            _observation_check(observation, _reference_merge(sample), require_unmutated=True)
        )
    return _score_case("interval_merge", 30, checks)


def _ttl_payload_and_expected(rng: random.Random) -> tuple[dict[str, object], list[object]]:
    scenarios = []
    expected: list[object] = ["ValueError", "ValueError"]
    for scenario_index in range(4):
        capacity = 1 + scenario_index
        ttl = 1.5 + scenario_index
        ref = _RefCache(capacity, ttl)
        operations = []
        now = 0.0
        for step in range(17):
            now += rng.random() * 1.4
            op = rng.choice(["set", "get", "get", "delete"])
            key = rng.choice(["a", "b", "c", "d", "e"])
            value = rng.randint(-100, 100)
            default = f"__CPTR_MISSING_{scenario_index}_{step}__"
            if op == "set":
                ref.set(key, value, now)
                expected.append(None)
                operations.append({"op": "set", "key": key, "value": value, "now": now})
            elif op == "get":
                expected.append(ref.get(key, now, default))
                operations.append({"op": "get", "key": key, "now": now, "default": default})
            else:
                expected.append(ref.delete(key))
                operations.append({"op": "delete", "key": key})
        scenarios.append({"capacity": capacity, "ttl": ttl, "operations": operations})
    return {
        "task": "ttl_lru_cache",
        "validations": [[0, 1], [1, 0]],
        "scenarios": scenarios,
    }, expected


async def _grade_ttl(root: Path, rng: random.Random, timeout: float) -> dict[str, object]:
    payload, expected = _ttl_payload_and_expected(rng)
    result = await _run_student(root, payload, timeout=timeout)
    checks: list[tuple[bool, str | None]] = []
    validations = result.get("validations") if isinstance(result.get("validations"), list) else []
    for index in range(2):
        observation = validations[index] if index < len(validations) else None
        ok = (
            isinstance(observation, dict)
            and observation.get("kind") == "exception"
            and observation.get("type") == expected[index]
        )
        checks.append((ok, None if ok else "validation"))

    cursor = 2
    scenarios = result.get("scenarios") if isinstance(result.get("scenarios"), list) else []
    for scenario_index in range(4):
        scenario = (
            scenarios[scenario_index]
            if scenario_index < len(scenarios) and isinstance(scenarios[scenario_index], dict)
            else {}
        )
        constructor = scenario.get("constructor") if isinstance(scenario, dict) else None
        observations = (
            scenario.get("observations") if isinstance(scenario.get("observations"), list) else []
        )
        operations = payload["scenarios"][scenario_index]["operations"]  # type: ignore[index]
        constructor_ok = isinstance(constructor, dict) and constructor.get("kind") == "return"
        for op_index, _operation in enumerate(operations):
            observation = (
                observations[op_index] if constructor_ok and op_index < len(observations) else None
            )
            checks.append(_observation_check(observation, expected[cursor]))
            cursor += 1
    return _score_case("ttl_lru_cache", 35, checks)


def _retry_payload_and_expected(
    rng: random.Random,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    scenarios: list[dict[str, object]] = [
        {"attempts": 0, "actions": [{"kind": "return", "value": 1}]},
        {"attempts": 4, "actions": [{"kind": "return", "value": "ok"}]},
        {
            "attempts": 5,
            "actions": [
                {"kind": "retryable", "message": "again"},
                {"kind": "retryable", "message": "again"},
                {"kind": "return", "value": 42},
            ],
        },
        {"attempts": 5, "actions": [{"kind": "fatal", "message": "stop"}]},
        {
            "attempts": 4,
            "actions": [
                {"kind": "retryable", "message": "1"},
                {"kind": "retryable", "message": "2"},
                {"kind": "retryable", "message": "3"},
                {"kind": "retryable", "message": "4"},
            ],
        },
    ]
    expected: list[dict[str, object]] = [
        {"kind": "exception", "type": "ValueError", "calls": 0},
        {"kind": "return", "value": "ok", "calls": 1},
        {"kind": "return", "value": 42, "calls": 3},
        {"kind": "exception", "type": "Fatal", "calls": 1},
        {"kind": "exception", "type": "Retryable", "message": "4", "calls": 4},
    ]
    for attempts in (1, 2, 7):
        success_at = rng.randint(1, attempts)
        actions = [{"kind": "retryable", "message": "retry"} for _ in range(success_at - 1)]
        actions.append({"kind": "return", "value": success_at})
        scenarios.append({"attempts": attempts, "actions": actions})
        expected.append({"kind": "return", "value": success_at, "calls": success_at})
    return {"task": "retry_policy", "scenarios": scenarios}, expected


async def _grade_retry(root: Path, rng: random.Random, timeout: float) -> dict[str, object]:
    payload, expected = _retry_payload_and_expected(rng)
    result = await _run_student(root, payload, timeout=timeout)
    observations = (
        result.get("observations") if isinstance(result.get("observations"), list) else []
    )
    checks: list[tuple[bool, str | None]] = []
    for index, wanted in enumerate(expected):
        observed = observations[index] if index < len(observations) else None
        if not isinstance(observed, dict):
            checks.append((False, "protocol"))
            continue
        ok = observed.get("kind") == wanted.get("kind") and observed.get("calls") == wanted.get(
            "calls"
        )
        if wanted.get("kind") == "return":
            ok = ok and observed.get("value") == wanted.get("value")
        else:
            ok = ok and observed.get("type") == wanted.get("type")
            if "message" in wanted:
                ok = ok and observed.get("message") == wanted.get("message")
        checks.append((ok, None if ok else str(observed.get("type") or "assertion")[:80]))
    return _score_case("retry_policy", 35, checks)


async def grade_workspace(root: Path, seed: str, *, timeout_seconds: float) -> dict[str, object]:
    """Grade one workspace without importing submission code into the scorer."""
    rng = random.Random(int(seed, 16))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, float(timeout_seconds))

    async def remaining() -> float:
        left = deadline - loop.time()
        if left <= 0:
            raise TimeoutError("benchmark grader timed out")
        return left

    interval = await _grade_interval(root, rng, await remaining())
    ttl = await _grade_ttl(root, rng, await remaining())
    retry = await _grade_retry(root, rng, await remaining())
    cases = [interval, ttl, retry]
    score = sum(int(case["points"]) for case in cases)
    if score < 0 or score > MAX_SCORE:
        raise RuntimeError("benchmark grader score is outside the allowed range")
    return {"score": score, "max_score": MAX_SCORE, "case_results": cases}
