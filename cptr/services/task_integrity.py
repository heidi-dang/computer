"""Deterministic completion-integrity checks for persisted CPTR task output."""

from __future__ import annotations

import json
from typing import Any

COMPLETE_WITH_TOOL_ERRORS = "COMPLETE_WITH_TOOL_ERRORS"
_TOOL_FAILURE_STATUSES = {"error", "failed", "failure"}


def _is_tool_failure_value(value: Any) -> bool:
    if isinstance(value, str):
        stripped = value.strip()
        lowered = stripped.lower()
        if lowered == "error" or lowered.startswith("error:"):
            return True
        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            try:
                return _is_tool_failure_value(json.loads(stripped))
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
        return False
    if isinstance(value, list):
        return any(_is_tool_failure_value(item) for item in value)
    if not isinstance(value, dict):
        return False

    status = str(value.get("status") or "").strip().lower()
    error = value.get("error")
    has_error_value = (
        error is not None
        and error is not False
        and (not isinstance(error, str) or bool(error.strip()))
    )
    return (
        status in _TOOL_FAILURE_STATUSES
        or value.get("ok") is False
        or value.get("success") is False
        or has_error_value
    )


def tool_error_count(raw_output: Any) -> int:
    """Count unique failed tool calls without treating ordinary assistant text as failure."""
    if not isinstance(raw_output, list):
        return 0

    failed_calls: set[str] = set()
    for index, item in enumerate(raw_output):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        call_id = str(item.get("call_id") or item.get("id") or f"{item_type}-{index}")
        if item_type == "function_call":
            status = str(item.get("status") or "").strip().lower()
            if status in _TOOL_FAILURE_STATUSES:
                failed_calls.add(call_id)
        elif item_type == "function_call_output" and _is_tool_failure_value(item.get("output")):
            failed_calls.add(call_id)
    return len(failed_calls)


def completion_integrity(raw_output: Any) -> dict[str, Any]:
    count = tool_error_count(raw_output)
    return {
        "status": "TOOL_ERRORS" if count else "CLEAN",
        "tool_error_count": count,
    }


def successful_terminal_status(raw_output: Any) -> str:
    """Return the truthful terminal status for an otherwise successful worker turn."""
    return COMPLETE_WITH_TOOL_ERRORS if tool_error_count(raw_output) else "COMPLETE"
