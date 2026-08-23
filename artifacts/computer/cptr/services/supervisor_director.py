"""Supervisor director implementations.

The local director is deliberately conservative and provider-neutral.  The
OpenAI Responses implementation can replace it without changing the monitor
state machine or API surface.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from cptr.services.supervisor import Decision


class LocalSupervisorDirector:
    async def evaluate(self, *, evidence: dict[str, Any], **kwargs: Any) -> Decision:
        task = evidence.get("task") or {}
        content = str(task.get("content") or "").strip()
        if not content:
            return Decision(
                defects=["worker produced no durable output"],
                next_action_required=True,
                next_assignment="Inspect the worker failure and produce durable output.",
            )
        return Decision(scope_satisfied=True, goal_satisfied=True)

    async def diagnose(self, *, failure: dict[str, Any], **kwargs: Any) -> Decision:
        return Decision(
            defects=[str(failure.get("message") or "verification failure")],
            next_action_required=True,
            next_assignment="Repair the reported verification failure and re-run checks.",
        )

    async def plan_next_action(self, *, decision: Decision, **kwargs: Any) -> Decision:
        return decision

    async def final_gate(self, *, scopes: list[Any], **kwargs: Any) -> Decision:
        if all(getattr(scope, "status", None).value == "VERIFIED" for scope in scopes):
            return Decision(scope_satisfied=True, goal_satisfied=True)
        return Decision(
            defects=["one or more scopes are not independently verified"],
            next_action_required=True,
            next_assignment="Repair every scope that is not independently verified.",
        )


class OpenAISupervisorDirector:
    """OpenAI Responses-backed director isolated behind the supervisor protocol."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("CPTR_SUPERVISOR_OPENAI_API_KEY", "")
        self.model = model or os.environ.get("CPTR_SUPERVISOR_OPENAI_MODEL", "")
        self.base_url = (
            base_url or os.environ.get("CPTR_OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._response_ids: dict[str, str] = {}
        if not self.api_key:
            raise ValueError("CPTR_SUPERVISOR_OPENAI_API_KEY is required")
        if not self.model:
            raise ValueError("CPTR_SUPERVISOR_OPENAI_MODEL is required")

    def state_for(self, monitor_id: str) -> dict[str, str]:
        response_id = self._response_ids.get(monitor_id)
        return {"last_response_id": response_id} if response_id else {}

    async def evaluate(
        self, *, monitor: Any, scope: Any, evidence: dict[str, Any], **kwargs: Any
    ) -> Decision:
        return await self._decide(
            "evaluate", monitor, {"scope": scope, "evidence": evidence, **kwargs}
        )

    async def diagnose(
        self, *, monitor: Any, scope: Any, failure: dict[str, Any], **kwargs: Any
    ) -> Decision:
        return await self._decide(
            "diagnose", monitor, {"scope": scope, "failure": failure, **kwargs}
        )

    async def plan_next_action(
        self, *, monitor: Any, scope: Any, decision: Decision, **kwargs: Any
    ) -> Decision:
        if decision.next_assignment:
            return decision
        return await self._decide(
            "plan_next_action", monitor, {"scope": scope, "decision": decision, **kwargs}
        )

    async def final_gate(self, *, monitor: Any, scopes: list[Any], **kwargs: Any) -> Decision:
        return await self._decide("final_gate", monitor, {"scopes": scopes, **kwargs})

    async def _decide(self, operation: str, monitor: Any, payload: dict[str, Any]) -> Decision:
        monitor_id = str(monitor.monitor_id)
        instructions = (
            "You are a software-engineering verification director. Return only the requested JSON decision. "
            "Use the immutable original goal and acceptance criteria as authoritative. Treat worker completion "
            "as evidence to inspect, never as proof of goal completion. Do not include hidden reasoning. "
            "The JSON may use either the full decision fields or a compact decision/status of PASS or FAIL."
        )
        input_payload = {
            "operation": operation,
            "original_goal": monitor.original_goal,
            "original_acceptance_criteria": monitor.original_acceptance_criteria,
            "payload": _json_safe(payload),
        }
        body = _director_request_body(
            model=self.model,
            instructions=instructions,
            input_text=json.dumps(input_payload),
        )
        try:
            async with (
                httpx.AsyncClient(timeout=self.timeout_seconds) as client,
                client.stream(
                    "POST",
                    f"{self.base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                ) as response,
            ):
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    response_id, decision_payload = _extract_sse_decision(
                        "\n".join([line async for line in response.aiter_lines()])
                    )
                else:
                    raw = json.loads((await response.aread()).decode())
                    response_id = raw.get("id")
                    decision_payload = _extract_json_payload(raw)
            if isinstance(response_id, str):
                self._response_ids[monitor_id] = response_id
            return _decision_from_payload(operation, decision_payload)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"supervisor director {operation} failed") from exc


def _json_safe(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "__dict__"):
        return {
            key: _json_safe(item)
            for key, item in value.__dict__.items()
            if key not in {"history", "verification_evidence", "failure_evidence"}
        }
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if key not in {"raw_output", "reasoning_details"}
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _director_request_body(*, model: str, instructions: str, input_text: str) -> dict[str, Any]:
    """Build a streaming request accepted by providers with partial Responses support.

    Some OpenAI-compatible Responses endpoints accept streaming output but do not
    implement strict ``text.format`` schemas.  The director still validates and
    normalizes the returned JSON locally, so the provider schema is advisory rather
    than a trust boundary.
    """

    return {
        "model": model,
        "store": True,
        "stream": True,
        "max_output_tokens": 4096,
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": input_text}],
            }
        ],
    }


def _extract_json_payload(response: dict[str, Any]) -> dict[str, Any]:
    for item in response.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
    raise ValueError("structured supervisor decision missing")


def _extract_sse_decision(payload: str) -> tuple[str | None, dict[str, Any]]:
    response_id: str | None = None
    output_text: list[str] = []
    terminal_text: list[str] = []
    output_item_text: list[str] = []
    for line in payload.splitlines():
        if not line.startswith("data:"):
            continue
        raw_line = line[5:].strip()
        if not raw_line or raw_line == "[DONE]":
            continue
        event = json.loads(raw_line)
        event_type = event.get("type")
        if event_type in {"response.created", "response.completed"}:
            response = event.get("response")
            if isinstance(response, dict) and isinstance(response.get("id"), str):
                response_id = response["id"]
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
            output_text.append(event["delta"])
        if event_type == "response.output_text.done" and isinstance(event.get("text"), str):
            terminal_text.append(event["text"])
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                for content in item.get("content", []):
                    if isinstance(content, dict) and isinstance(content.get("text"), str):
                        output_item_text.append(content["text"])
        if event_type == "response.failed":
            raise RuntimeError("supervisor director response failed")
    text = terminal_text or output_text or output_item_text
    return response_id, _parse_decision_text("".join(text))


def _parse_decision_text(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise TypeError("structured supervisor decision must be an object")
    return parsed


def _decision_from_payload(operation: str, payload: dict[str, Any]) -> Decision:
    if "scope_satisfied" in payload and "goal_satisfied" in payload:
        return Decision(
            scope_satisfied=bool(payload["scope_satisfied"]),
            goal_satisfied=bool(payload["goal_satisfied"]),
            defects=[str(item) for item in payload.get("defects") or []],
            regressions=[str(item) for item in payload.get("regressions") or []],
            next_action_required=bool(payload.get("next_action_required", False)),
            next_assignment=payload.get("next_assignment"),
            blocking_reason=payload.get("blocking_reason"),
        )

    status = str(payload.get("decision") or payload.get("status") or "").strip().upper()
    reason = str(payload.get("reason") or payload.get("message") or "").strip()
    accepted = status in {"PASS", "PASSED", "SUCCESS", "ACCEPT", "ACCEPTED", "COMPLETE"}
    if accepted:
        return Decision(scope_satisfied=True, goal_satisfied=True)
    if not status:
        raise ValueError("structured supervisor decision missing required fields")
    detail = reason or f"supervisor director returned {status}"
    return Decision(
        defects=[detail],
        next_action_required=True,
        next_assignment=(
            "Repair the reported director failure and re-run independent verification."
            if operation != "final_gate"
            else "Repair the reported final-gate failure and re-run the final gate."
        ),
        blocking_reason=detail if operation == "final_gate" else None,
    )
