"""Supervisor director implementations.

The local director is deliberately conservative and provider-neutral.  The
OpenAI Responses implementation can replace it without changing the monitor
state machine or API surface.
"""

from __future__ import annotations

import json
import os
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
            "as evidence to inspect, never as proof of goal completion. Do not include hidden reasoning."
        )
        input_payload = {
            "operation": operation,
            "original_goal": monitor.original_goal,
            "original_acceptance_criteria": monitor.original_acceptance_criteria,
            "payload": _json_safe(payload),
        }
        body: dict[str, Any] = {
            "model": self.model,
            "store": True,
            "instructions": instructions,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": json.dumps(input_payload)}],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "supervisor_decision",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "scope_satisfied": {"type": "boolean"},
                            "goal_satisfied": {"type": "boolean"},
                            "defects": {"type": "array", "items": {"type": "string"}},
                            "regressions": {"type": "array", "items": {"type": "string"}},
                            "next_action_required": {"type": "boolean"},
                            "next_assignment": {"type": ["string", "null"]},
                            "blocking_reason": {"type": ["string", "null"]},
                        },
                        "required": [
                            "scope_satisfied",
                            "goal_satisfied",
                            "defects",
                            "regressions",
                            "next_action_required",
                            "next_assignment",
                            "blocking_reason",
                        ],
                    },
                }
            },
        }
        previous = self._response_ids.get(monitor_id) or (monitor.director_state or {}).get(
            "last_response_id"
        )
        if previous:
            body["previous_response_id"] = previous
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            response.raise_for_status()
            raw = response.json()
            response_id = raw.get("id")
            if isinstance(response_id, str):
                self._response_ids[monitor_id] = response_id
            decision_payload = _extract_json_payload(raw)
            return Decision(
                scope_satisfied=bool(decision_payload["scope_satisfied"]),
                goal_satisfied=bool(decision_payload["goal_satisfied"]),
                defects=[str(item) for item in decision_payload["defects"]],
                regressions=[str(item) for item in decision_payload["regressions"]],
                next_action_required=bool(decision_payload["next_action_required"]),
                next_assignment=decision_payload["next_assignment"],
                blocking_reason=decision_payload["blocking_reason"],
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"supervisor director {operation} failed") from exc


def _json_safe(value: Any) -> Any:
    if hasattr(value, "__dict__"):
        return {
            key: _json_safe(item) for key, item in value.__dict__.items() if key not in {"history"}
        }
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _extract_json_payload(response: dict[str, Any]) -> dict[str, Any]:
    for item in response.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
    raise ValueError("structured supervisor decision missing")
