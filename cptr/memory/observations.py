"""Server-authoritative promotion of high-value execution outcomes into Memory Core.

This boundary deliberately does not persist every MCP action. It accepts only completed,
backend-observed outcomes and promotes a small class of future-useful procedures/failures.
Raw command output is never stored as canonical memory; failure text is reduced to one
bounded, redacted signature.
"""

from __future__ import annotations

import re
from typing import Any

from cptr.memory.service import EmbeddedMemoryService, get_memory_service
from cptr.utils.redaction import redact_external_text, redact_sensitive

_HIGH_VALUE_COMMAND_RE = re.compile(
    r"(?:^|\s)(?:"
    r"pytest|vitest|jest|npm\s+(?:test|run\s+(?:test|build|check|lint))|"
    r"pnpm\s+(?:test|run\s+(?:test|build|check|lint))|"
    r"yarn\s+(?:test|run\s+(?:test|build|check|lint))|"
    r"python(?:3)?\s+-m\s+(?:pytest|unittest)|"
    r"systemctl\s+(?:restart|reload|try-restart)|service\s+\S+\s+(?:restart|reload)|"
    r"docker\s+compose\s+(?:up|restart|build)|docker-compose\s+(?:up|restart|build)|"
    r"(?:alembic|prisma)\s+(?:migrate|upgrade)|"
    r"(?:deploy|smoke|verify|verification|healthcheck|release)\b"
    r")",
    re.IGNORECASE,
)
_FAILURE_SIGNAL_RE = re.compile(
    r"\b(?:error|failed|failure|exception|traceback|denied|timeout|timed out|"
    r"not found|refused|unhealthy|invalid|conflict|forbidden|unauthori[sz]ed|"
    r"4\d\d|5\d\d)\b",
    re.IGNORECASE,
)
_MAX_COMMAND_CHARS = 2_000
_MAX_SIGNATURE_CHARS = 600


def _bounded_command(command: str) -> str:
    value = redact_external_text(str(command or "").strip())
    if len(value) > _MAX_COMMAND_CHARS:
        return value[: _MAX_COMMAND_CHARS - 1].rstrip() + "…"
    return value


def _failure_signature(output: str) -> str:
    lines = [
        redact_external_text(line.strip())
        for line in str(output or "").splitlines()
        if line.strip()
    ]
    if not lines:
        return "command returned a non-zero exit code"
    selected = next(
        (line for line in reversed(lines) if _FAILURE_SIGNAL_RE.search(line)), lines[-1]
    )
    selected = re.sub(r"\s+", " ", selected).strip()
    if len(selected) > _MAX_SIGNATURE_CHARS:
        selected = selected[: _MAX_SIGNATURE_CHARS - 1].rstrip() + "…"
    return selected or "command returned a non-zero exit code"


def _is_future_useful(*, action: str, command: str, metadata: dict[str, Any]) -> bool:
    if action == "test":
        return True
    if bool(metadata.get("force_future_utility")):
        return True
    return bool(_HIGH_VALUE_COMMAND_RE.search(command))


async def observe_execution_outcome(
    *,
    user_id: str,
    workspace: str,
    action: str,
    command: str,
    status: str,
    exit_code: int | None,
    output: str = "",
    metadata: dict[str, Any] | None = None,
    service: EmbeddedMemoryService | None = None,
) -> str | None:
    """Promote one completed, useful backend outcome and return its consolidation job ID.

    The caller must supply an authenticated owner/workspace and an outcome produced by the
    backend itself. RUNNING/interrupted/noisy commands are ignored. The canonical memory is
    deliberately terse so retrieval learns procedures and failure signatures rather than logs.
    """

    owner = str(user_id or "").strip()
    workspace_path = str(workspace or "").strip()
    normalized_action = str(action or "command").strip().lower() or "command"
    normalized_status = str(status or "").strip().upper()
    details = redact_sensitive(metadata or {})
    safe_command = _bounded_command(command)

    if not owner or not workspace_path or normalized_status != "COMPLETE":
        return None
    if exit_code is None or not safe_command:
        return None
    if not _is_future_useful(action=normalized_action, command=safe_command, metadata=details):
        return None

    target = str(details.get("target") or "").strip()
    transport = str(details.get("transport") or normalized_action).strip()[:80]
    if int(exit_code) == 0:
        kind = "procedure"
        heading = f"Verified {transport} procedure"
        if normalized_action == "test" and target:
            canonical_text = (
                f"Verified validation procedure: target `{target}` completed successfully "
                f"using `{safe_command}`."
            )
        else:
            canonical_text = (
                f"Verified operational procedure: `{safe_command}` completed successfully "
                "with exit code 0."
            )
        confidence = 0.98
        importance = 0.72
    else:
        kind = "failure"
        heading = f"Observed {transport} failure"
        signature = _failure_signature(output)
        canonical_text = (
            f"Observed operational failure: `{safe_command}` exited with code {int(exit_code)}. "
            f"Failure signature: {signature}."
        )
        confidence = 0.95
        importance = 0.78

    memory_service = service or get_memory_service()
    event_id = await memory_service.record_event(
        user_id=owner,
        workspace=workspace_path,
        event_type="observation_promoted",
        scope="workspace",
        heading=heading,
        reason="completed backend outcome crossed the persistent-memory future-utility threshold",
        trust_level="verified_system_fact",
        confidence_ppm=int(confidence * 1_000_000),
        payload={
            "action": normalized_action,
            "transport": transport,
            "target": target or None,
            "exit_code": int(exit_code),
            "command": safe_command,
        },
    )
    return await memory_service.queue_consolidation(
        user_id=owner,
        workspace=workspace_path,
        scope="workspace",
        text=canonical_text,
        heading=heading,
        kind=kind,
        structured_value={
            "observation": {
                "action": normalized_action,
                "transport": transport,
                "target": target or None,
                "exit_code": int(exit_code),
            }
        },
        source_event_ids=[event_id],
        trust_level="verified_system_fact",
        confidence=confidence,
        importance=importance,
    )


__all__ = ["observe_execution_outcome"]
