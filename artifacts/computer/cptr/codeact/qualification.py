"""Live same-model CodeAct qualification with a deterministic read-only corpus.

This module deliberately keeps the benchmark corpus isolated from a real user
workspace.  It verifies the provider's native tool-call protocol and CodeAct
program generation against identical, fixed data while the runtime's existing
tool-policy adapter remains responsible for production workspace access.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from cptr.codeact.benchmark import BenchmarkCase, BenchmarkReport, ProviderMeasurement, run_provider_benchmark
from cptr.codeact.contracts import CodeActConfig, CodeActIdentity, CodeActMode
from cptr.codeact.repl import CodeActRepl, ReadOnlyCapabilitySDK
from cptr.utils.config import _get_jwt_secret
from cptr.utils.crypto import decrypt_key
from cptr.utils.model_targets import ApiModelTarget, first_api_model_target, resolve_model_target

FINAL_PREFIX = "FINAL:"

QUALIFICATION_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        "release-label",
        "Read release.txt. What is the value after release=?",
        "ORCHID",
    ),
    BenchmarkCase(
        "inventory-total",
        "Read inventory.txt. What is the sum of the apples and oranges values?",
        "18",
    ),
    BenchmarkCase(
        "ready-owner",
        "Read ownership.txt. What is the owner value on the record whose status is ready?",
        "Lin",
    ),
)

FIXTURE_FILES = {
    "release.txt": "release=ORCHID\nchannel=stable\n",
    "inventory.txt": "apples=7\noranges=11\nbananas=2\n",
    "ownership.txt": "status=ready\nowner=Lin\nregion=ap-southeast\n",
}

NATIVE_TOOLS = (
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a named read-only fixture file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List the available read-only fixture files.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
)


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _final_value(text: str) -> str:
    """Extract a deliberately narrow final answer from a model or worker output."""
    match = re.search(rf"(?im)^\s*{re.escape(FINAL_PREFIX)}\s*(.+?)\s*$", text or "")
    return match.group(1).strip() if match else ""


def _program_from_response(text: str) -> str:
    """Accept plain source or a single Markdown fence, rejecting prose wrappers."""
    text = (text or "").strip()
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    fenced = re.fullmatch(r"```(?:python)?\s*\n?(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    return (fenced.group(1) if fenced else text).strip()


@dataclass
class FixtureReadOnlyTools:
    """Small, explicit corpus data with no filesystem or workspace authority."""

    files: dict[str, str]

    async def invoke(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "read_file":
            path = str(arguments.get("path", ""))
            if path not in self.files:
                raise ValueError(f"fixture file not found: {path}")
            return self.files[path]
        if name == "list_directory":
            return "\n".join(sorted(self.files))
        raise PermissionError(f"fixture capability denied: {name}")

    def codeact_sdk(self) -> ReadOnlyCapabilitySDK:
        async def read(**kwargs: Any) -> str:
            return await self.invoke("read_file", kwargs)

        async def list_files(**kwargs: Any) -> str:
            return await self.invoke("list_directory", kwargs)

        return ReadOnlyCapabilitySDK.from_handlers({"files.read": read, "files.list": list_files})


class OpenAICompatibleQualificationRunner:
    """One resolved CPTR API model used by both benchmark arms."""

    def __init__(self, target: ApiModelTarget, fixtures: FixtureReadOnlyTools) -> None:
        self.target = target
        self.fixtures = fixtures

    async def _complete(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        connection = self.target.connection
        endpoint = f"{(connection.get('base_url') or '').rstrip('/')}/chat/completions"
        if not endpoint.startswith("https://"):
            raise ValueError("qualification requires an HTTPS OpenAI-compatible base URL")
        api_key = decrypt_key(str(connection.get("api_key") or ""), _get_jwt_secret())
        if not api_key:
            raise ValueError("resolved provider has no API key")
        async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        response.raise_for_status()
        return response.json(), _json_bytes(payload)

    @staticmethod
    def _usage(data: dict[str, Any]) -> tuple[int, int, int]:
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        return input_tokens, output_tokens, int(usage.get("total_tokens") or input_tokens + output_tokens)

    async def __call__(
        self, case: BenchmarkCase, mode: CodeActMode, _telemetry: Any
    ) -> ProviderMeasurement:
        if mode is CodeActMode.DISABLED:
            return await self._run_native(case)
        if mode is CodeActMode.READ_ONLY:
            return await self._run_codeact(case)
        raise ValueError(f"unsupported qualification mode: {mode}")

    async def _run_native(self, case: BenchmarkCase) -> ProviderMeasurement:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are completing a deterministic read-only fixture task. "
                    "Use only the supplied functions to inspect the fixture. "
                    f"After the function results, reply on one line as {FINAL_PREFIX} <answer>. "
                    "Do not guess values before reading a fixture."
                ),
            },
            {"role": "user", "content": case.prompt},
        ]
        input_tokens = output_tokens = total_tokens = context_bytes = calls = cycles = 0
        for _round in range(4):
            payload = {
                "model": self.target.runtime_model,
                "messages": messages,
                "tools": list(NATIVE_TOOLS),
                "max_completion_tokens": 512,
            }
            data, request_bytes = await self._complete(payload)
            in_tokens, out_tokens, tokens = self._usage(data)
            input_tokens += in_tokens
            output_tokens += out_tokens
            total_tokens += tokens
            context_bytes += request_bytes
            cycles += 1
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return ProviderMeasurement(
                    result=_final_value(str(message.get("content") or "")),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    cycles=cycles,
                    capability_calls=calls,
                    context_bytes=context_bytes,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    raise ValueError(f"native tool arguments were invalid JSON for {name}") from exc
                result = await self.fixtures.invoke(name, arguments)
                calls += 1
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}
                )
        raise RuntimeError("native tool-calling did not produce a final answer within four rounds")

    async def _run_codeact(self, case: BenchmarkCase) -> ProviderMeasurement:
        sdk = self.fixtures.codeact_sdk()
        prompt = {
            "model": self.target.runtime_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Write only a valid Python program for a restricted read-only CodeAct worker. "
                        "Use only cptr.files.read(path=...) and cptr.files.list(). "
                        "Do not import modules, use Markdown, or include explanation. "
                        f"Your program must inspect the fixture and print exactly {FINAL_PREFIX} <answer>."
                    ),
                },
                {"role": "user", "content": case.prompt},
            ],
            "max_completion_tokens": 768,
        }
        data, context_bytes = await self._complete(prompt)
        input_tokens, output_tokens, total_tokens = self._usage(data)
        source = _program_from_response(
            str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        )
        identity = CodeActIdentity(
            user_id="codeact-qualification",
            workspace="deterministic-fixture",
            task_id=f"qualification-{case.name}",
            attempt_id=f"qualification-{case.name}",
            model_id=self.target.full_model_id,
        )
        config = CodeActConfig(
            mode=CodeActMode.EVALUATION,
            allowed_roles=frozenset({"qualification"}),
        )
        repl = CodeActRepl(identity=identity, sdk=sdk, config=config)
        try:
            result = await repl.execute(source)
            value = _final_value(result.output)
        except Exception as exc:
            value = f"ERROR: {type(exc).__name__}: {str(exc)[:200]}"
        finally:
            capability_calls = len(repl.capability_calls)
            await repl.close(force=True)
        return ProviderMeasurement(
            result=value,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cycles=1,
            capability_calls=capability_calls,
            context_bytes=context_bytes,
        )


async def run_live_qualification(model_id: str = "") -> BenchmarkReport:
    """Run the full provider-backed qualification against one resolved API model."""
    if model_id.strip():
        resolved = await resolve_model_target(model_id.strip())
        if not isinstance(resolved, ApiModelTarget):
            raise ValueError("CodeAct qualification requires an OpenAI-compatible API model")
        target = resolved
    else:
        target = await first_api_model_target()
    await _validate_qualification_target(target)
    runner = OpenAICompatibleQualificationRunner(target, FixtureReadOnlyTools(dict(FIXTURE_FILES)))
    return await run_provider_benchmark(
        QUALIFICATION_CASES,
        model_id=target.full_model_id,
        provider_runner=runner,
        provider_backed=True,
    )


async def _validate_qualification_target(target: ApiModelTarget) -> None:
    """Reject API types and model ids that cannot be honestly compared here."""
    connection = target.connection
    if connection.get("provider") != "openai" or connection.get("api_type", "chat_completions") != "chat_completions":
        raise ValueError("qualification requires a configured OpenAI chat-completions connection")
    base_url = str(connection.get("base_url") or "").rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("qualification requires an HTTPS OpenAI-compatible base URL")
    from cptr.routers.chat import _fetch_provider_models

    available_models = connection.get("data", {}).get("models") or await _fetch_provider_models(connection)
    if target.runtime_model not in (available_models or []):
        raise ValueError("resolved model is not available from the configured provider connection")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CPTR's live CodeAct qualification.")
    parser.add_argument("--model", default="", help="Configured CPTR API model id (default: first resolved API model)")
    parser.add_argument(
        "--report",
        default="docs/codeact-qualification-report.json",
        help="Path for the JSON report, relative to the CPTR project root",
    )
    args = parser.parse_args(argv)
    report = asyncio.run(run_live_qualification(args.model))
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.to_json() + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "decision": report.decision, "score": report.score}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())