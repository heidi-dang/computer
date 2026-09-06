"""Server-authoritative promotion of high-value execution outcomes into Memory Core.

This boundary deliberately does not persist every MCP action. It accepts only completed,
backend-observed outcomes and promotes a small class of future-useful procedures/failures.
Raw command output is never stored as canonical memory; failure text is reduced to one
bounded, redacted signature.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from cptr.memory.domain import RetrievalFeedback
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
_FEEDBACK_WINDOW_MS = 15 * 60 * 1000
_MAX_FEEDBACK_OUTCOME_ITEMS = 3
_ENV_PREFIX_RE = re.compile(r'^(?:(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|\'[^\']*\'|\S+))\s+)+')
_VARIANT_FLAG_RE = re.compile(
    r"(?<!\S)(?:-q|-v{1,3}|--quiet|--verbose|--no-color|--color(?:=\S+)?)(?=\s|$)",
    re.IGNORECASE,
)


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


def _semantic_command_family(command: str) -> str:
    """Normalize incidental invocation differences without erasing the actual target."""
    value = _ENV_PREFIX_RE.sub("", str(command or "").strip())
    value = _VARIANT_FLAG_RE.sub("", value)
    return re.sub(r"\s+", " ", value).strip().lower()[:1_000]


def _observation_signature(
    *,
    kind: str,
    action: str,
    transport: str,
    target: str,
    command: str,
    failure_signature: str = "",
) -> tuple[str, str]:
    family = _semantic_command_family(command)
    source = "|".join(
        [
            kind.strip().lower(),
            action.strip().lower(),
            transport.strip().lower(),
            target.lower(),
            family,
        ]
    )
    if failure_signature:
        source += "|failure:" + re.sub(r"\s+", " ", failure_signature).strip().lower()
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32], family


async def _matching_observation_memory(
    *,
    service: EmbeddedMemoryService,
    user_id: str,
    workspace: str,
    kind: str,
    signature: str,
) -> dict[str, Any] | None:
    rows = await service.store.list_candidates(
        user_id=user_id,
        workspace=workspace,
        include_historical=False,
        scope="workspace",
        limit=500,
    )
    for row in rows:
        if str(row.get("kind") or "") != kind:
            continue
        structured = (
            row.get("structured_value") if isinstance(row.get("structured_value"), dict) else {}
        )
        observation = (
            structured.get("observation") if isinstance(structured.get("observation"), dict) else {}
        )
        if str(observation.get("signature") or "") == signature:
            return row
    return None


async def _reinforce_recent_recall(
    *,
    service: EmbeddedMemoryService,
    user_id: str,
    workspace: str,
    outcome: str,
) -> None:
    """Attach a real backend terminal outcome to the most recent MCP recall context."""
    try:
        events = await service.event_store.list_events(user_id, workspace=workspace, limit=40)
        now_ms = int(time.time() * 1000)
        recalled: dict[str, Any] | None = None
        for event in events:
            if str(event.get("event_type") or "") != "recall":
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            context_id = str(payload.get("feedback_context_id") or "").strip()
            if not context_id:
                continue
            created_at_ms = int(event.get("created_at_ms") or 0)
            if created_at_ms and now_ms - created_at_ms > _FEEDBACK_WINDOW_MS:
                continue
            recalled = event
            break
        if recalled is None:
            return
        payload = recalled.get("payload") if isinstance(recalled.get("payload"), dict) else {}
        context_id = str(payload.get("feedback_context_id") or "").strip()
        if await service.store.feedback_context_has_outcome(
            user_id=user_id,
            workspace=workspace,
            context_id=context_id,
        ):
            return
        query_hash = str(payload.get("query_hash") or "")[:128]
        items = (
            payload.get("feedback_items") if isinstance(payload.get("feedback_items"), list) else []
        )
        helpful = outcome == "success"
        for item in items[:_MAX_FEEDBACK_OUTCOME_ITEMS]:
            if not isinstance(item, dict) or bool(item.get("verification_stale")):
                continue
            memory_id = str(item.get("memory_id") or "").strip()
            if not memory_id:
                continue
            features = item.get("features") if isinstance(item.get("features"), dict) else {}
            await service.feedback(
                RetrievalFeedback(
                    user_id=user_id,
                    workspace=workspace,
                    memory_id=memory_id,
                    context_id=context_id,
                    query=f"sha256:{query_hash}",
                    rank=max(1, int(item.get("rank") or 1)),
                    score=max(0.0, min(1.0, float(item.get("score") or 0.0))),
                    used=True,
                    helpful=helpful,
                    outcome=outcome,
                    features={
                        str(key): max(0.0, min(1.0, float(value)))
                        for key, value in features.items()
                        if isinstance(value, (int, float))
                    },
                )
            )
    except Exception:
        # Feedback learning is derived and must never alter command truth or availability.
        return


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
    failure_signature = ""
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
        outcome = "success"
    else:
        kind = "failure"
        heading = f"Observed {transport} failure"
        failure_signature = _failure_signature(output)
        canonical_text = (
            f"Observed operational failure: `{safe_command}` exited with code {int(exit_code)}. "
            f"Failure signature: {failure_signature}."
        )
        confidence = 0.95
        importance = 0.78
        outcome = "failure"

    observation_signature, command_family = _observation_signature(
        kind=kind,
        action=normalized_action,
        transport=transport,
        target=target,
        command=safe_command,
        failure_signature=failure_signature,
    )
    memory_service = service or get_memory_service()
    existing = await _matching_observation_memory(
        service=memory_service,
        user_id=owner,
        workspace=workspace_path,
        kind=kind,
        signature=observation_signature,
    )
    if existing is not None:
        memory_id = str(existing.get("memory_id") or "")
        if memory_id:
            await memory_service.verify(memory_id, user_id=owner, workspace=workspace_path)
            await memory_service.intelligence_store.record_outcome(memory_id, outcome=outcome)
            await memory_service.record_event(
                user_id=owner,
                workspace=workspace_path,
                event_type="observation_reused",
                scope="workspace",
                memory_id=memory_id,
                heading=heading,
                reason="semantic observation matched an existing canonical outcome",
                trust_level="verified_system_fact",
                confidence_ppm=int(confidence * 1_000_000),
                payload={
                    "action": normalized_action,
                    "transport": transport,
                    "target": target or None,
                    "exit_code": int(exit_code),
                    "signature": observation_signature,
                },
            )
        await _reinforce_recent_recall(
            service=memory_service,
            user_id=owner,
            workspace=workspace_path,
            outcome=outcome,
        )
        return None

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
            "signature": observation_signature,
        },
    )
    job_id = await memory_service.queue_consolidation(
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
                "signature": observation_signature,
                "command_family": command_family,
            }
        },
        source_event_ids=[event_id],
        trust_level="verified_system_fact",
        confidence=confidence,
        importance=importance,
    )
    await _reinforce_recent_recall(
        service=memory_service,
        user_id=owner,
        workspace=workspace_path,
        outcome=outcome,
    )
    return job_id


__all__ = ["observe_execution_outcome"]
