"""One read-only CodeAct attempt with native-style lifecycle events."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from cptr.codeact.contracts import CodeActConfig, CodeActIdentity, CodeActResult
from cptr.codeact.repl import CodeActRepl, ReadOnlyCapabilitySDK

EventSink = Callable[[dict[str, Any]], Awaitable[None]]


async def run_read_only_attempt(
    *,
    identity: CodeActIdentity,
    sdk: ReadOnlyCapabilitySDK,
    program: str,
    config: CodeActConfig,
    role: str = "security-auditor",
    emit: EventSink | None = None,
) -> CodeActResult:
    """Run exactly one generated block sequence and always destroy its REPL.

    Model generation is deliberately outside this function. CPTR remains the
    sole model owner; this function only executes a server-approved program
    inside one attempt-scoped worker.
    """

    if not config.allows_role(role):
        raise PermissionError("CodeAct read-only execution is disabled for this role")

    async def event(kind: str, **payload: Any) -> None:
        if emit:
            await emit(
                {
                    "type": kind,
                    "task_id": identity.task_id,
                    "run_id": identity.run_id,
                    "step_id": identity.step_id,
                    "operation_id": identity.operation_id,
                    "attempt_id": identity.attempt_id,
                    "repl_session_id": identity.repl_session_id,
                    **payload,
                }
            )

    await event("codeact_started", capabilities=sorted(sdk.names))
    repl = CodeActRepl(identity=identity, sdk=sdk, config=config)
    try:
        result = await repl.execute(program)
        await event(
            "codeact_completed",
            execution_id=result.execution_id,
            capability_calls=len(result.capability_calls),
            output=result.output,
        )
        return result
    except asyncio.CancelledError:
        await repl.close(force=True)
        await event("codeact_cancelled")
        raise
    except Exception as exc:
        await event("codeact_failed", error=f"{type(exc).__name__}: {str(exc)[:500]}")
        raise
    finally:
        await repl.close(force=True)
