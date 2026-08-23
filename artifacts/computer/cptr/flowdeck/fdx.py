"""Optional, jailed FDX accelerator boundary.

FDX is never required for FlowDeck operation and never owns CPTR model/tool
execution. This adapter only transports structured, already-authorized work.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FDX_PROTOCOL = "flowdeck-fdx/1"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024


class FDXPolicyError(RuntimeError):
    """Raised when FDX configuration or output fails its safety contract."""


@dataclass(frozen=True)
class FDXConfig:
    enabled: bool = False
    executable: str = ""
    protocol: str = FDX_PROTOCOL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    @classmethod
    def from_env(cls) -> FDXConfig:
        return cls(
            enabled=os.getenv("CPTR_FLOWDECK_FDX_ENABLED", "").lower() == "true",
            executable=os.getenv("CPTR_FLOWDECK_FDX_EXECUTABLE", ""),
            protocol=os.getenv("CPTR_FLOWDECK_FDX_PROTOCOL", FDX_PROTOCOL),
            timeout_seconds=float(
                os.getenv("CPTR_FLOWDECK_FDX_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
            ),
            max_output_bytes=int(
                os.getenv("CPTR_FLOWDECK_FDX_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES)
            ),
        )


@dataclass(frozen=True)
class FDXResult:
    status: str
    output: dict[str, Any] | None
    authoritative: bool
    used_fdx: bool
    fallback_reason: str | None = None


def _validate_config(config: FDXConfig) -> None:
    if not config.enabled:
        return
    if config.protocol != FDX_PROTOCOL:
        raise FDXPolicyError("FDX protocol is incompatible")
    if not config.executable:
        raise FDXPolicyError("FDX executable is required when enabled")
    if config.timeout_seconds <= 0 or config.timeout_seconds > 300:
        raise FDXPolicyError("FDX timeout is outside the safe bound")
    if config.max_output_bytes <= 0 or config.max_output_bytes > 1024 * 1024:
        raise FDXPolicyError("FDX output bound is outside the safe limit")


def validate_workspace_jail(workspace: str, configured_root: str | None = None) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise FDXPolicyError("FDX workspace is not a directory")
    if configured_root:
        jail = Path(configured_root).expanduser().resolve()
        try:
            root.relative_to(jail)
        except ValueError as exc:
            raise FDXPolicyError("FDX workspace escapes configured jail") from exc
    return root


async def run_fdx(
    payload: dict[str, Any],
    *,
    workspace: str,
    config: FDXConfig,
    configured_root: str | None = None,
) -> FDXResult:
    """Run only a structured FDX process and return bounded, untrusted output."""
    _validate_config(config)
    root = validate_workspace_jail(workspace, configured_root)
    executable = str(Path(config.executable).expanduser())
    if not Path(executable).is_absolute():
        raise FDXPolicyError("FDX executable must be an absolute path")

    process = await asyncio.create_subprocess_exec(
        executable,
        "--protocol",
        config.protocol,
        "--workspace",
        str(root),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(root),
        env={"PATH": "/usr/bin:/bin", "HOME": str(root)},
    )
    request = json.dumps(payload, separators=(",", ":")).encode()
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(request),
            timeout=config.timeout_seconds,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if len(stdout) > config.max_output_bytes or len(stderr) > config.max_output_bytes:
        raise FDXPolicyError("FDX output exceeded configured bound")
    if process.returncode != 0:
        raise FDXPolicyError(f"FDX exited with status {process.returncode}")
    try:
        response = json.loads(stdout.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FDXPolicyError("FDX returned invalid structured output") from exc
    if not isinstance(response, dict) or response.get("protocol") != FDX_PROTOCOL:
        raise FDXPolicyError("FDX response protocol mismatch")
    return FDXResult(
        status="succeeded",
        output=response,
        authoritative=False,
        used_fdx=True,
    )


async def run_optional_fdx(
    payload: dict[str, Any],
    *,
    workspace: str,
    config: FDXConfig,
    fallback: Callable[[], Awaitable[FDXResult]],
    configured_root: str | None = None,
) -> FDXResult:
    """Use FDX only when qualified; otherwise preserve the secure CPTR path."""
    if not config.enabled:
        return await fallback()
    try:
        return await run_fdx(
            payload,
            workspace=workspace,
            config=config,
            configured_root=configured_root,
        )
    except (FDXPolicyError, OSError):
        fallback_result = await fallback()
        return FDXResult(
            status=fallback_result.status,
            output=fallback_result.output,
            authoritative=fallback_result.authoritative,
            used_fdx=False,
            fallback_reason="fdx_unavailable_or_failed",
        )