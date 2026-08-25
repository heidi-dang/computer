"""Optional, jailed FDX accelerator boundary.

FDX is never required for FlowDeck operation and never owns CPTR model/tool
execution. This adapter only transports structured, already-authorized work.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cptr.flowdeck.durable import DurableFlowDeck

FDX_PROTOCOL = "flowdeck-fdx/1"
FDX_VERSION = "1"
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
    max_input_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    read_only_verified: bool = False

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
            max_input_bytes=int(
                os.getenv("CPTR_FLOWDECK_FDX_MAX_INPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES)
            ),
        )


@dataclass(frozen=True)
class FDXResult:
    status: str
    output: dict[str, Any] | None
    authoritative: bool
    used_fdx: bool
    fallback_reason: str | None = None


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _restore_files(
    root: Path,
    snapshot: dict[str, bytes],
    *,
    expected_current: dict[str, bytes],
) -> None:
    """Restore a snapshot only when the workspace still has the observed state.

    The extra comparison closes the snapshot/restore gap.  A process outside
    FDX may change the workspace after the side-effect snapshot was taken; in
    that case cleanup must stop rather than overwrite its newer data.
    """
    if _snapshot_files(root) != expected_current:
        raise FDXPolicyError("workspace changed during FDX cleanup")
    current = {
        str(path.relative_to(root)): path
        for path in root.rglob("*")
        if path.is_file()
    }
    for relative, path in current.items():
        if relative not in snapshot:
            if (
                relative not in expected_current
                or path.read_bytes() != expected_current[relative]
            ):
                raise FDXPolicyError("workspace changed during FDX cleanup")
            path.unlink()
    for relative, content in snapshot.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.read_bytes() != expected_current.get(relative):
            raise FDXPolicyError("workspace changed during FDX cleanup")
        path.write_bytes(content)


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
    if config.max_input_bytes <= 0 or config.max_input_bytes > 1024 * 1024:
        raise FDXPolicyError("FDX input bound is outside the safe limit")
    if not config.read_only_verified:
        raise FDXPolicyError("FDX read-only parity is not verified")


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
    store: DurableFlowDeck | None = None,
    run_id: str | None = None,
    owner: str = "flowdeck-fdx",
    lease_ttl_ms: int = 120_000,
) -> FDXResult:
    """Run only a structured FDX process and return bounded, untrusted output."""
    _validate_config(config)
    root = validate_workspace_jail(workspace, configured_root)
    if not configured_root:
        raise FDXPolicyError("FDX requires an explicit configured jail")
    if store is None or not run_id:
        raise FDXPolicyError("FDX requires a durable workspace lease")
    lease = await store.acquire_workspace_lease(
        workspace=str(root),
        run_id=run_id,
        owner=owner,
        ttl_ms=lease_ttl_ms,
    )
    if lease is None:
        raise FDXPolicyError("FDX workspace is already owned")
    jail_root = Path(configured_root).expanduser().resolve()
    executable = str(Path(config.executable).expanduser())
    if not Path(executable).is_absolute():
        raise FDXPolicyError("FDX executable must be an absolute path")
    if configured_root:
        executable_path = Path(executable).resolve()
        jail = Path(configured_root).expanduser().resolve()
        try:
            executable_path.relative_to(jail)
        except ValueError as exc:
            raise FDXPolicyError("FDX executable escapes configured jail") from exc
    try:
        encoded_request = json.dumps(payload, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise FDXPolicyError("FDX payload is not structured JSON") from exc
    if len(encoded_request) > config.max_input_bytes:
        raise FDXPolicyError("FDX input exceeded configured bound")
    before = _snapshot_files(jail_root)

    try:
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
            limit=config.max_output_bytes + 1,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(encoded_request),
                timeout=config.timeout_seconds,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            raise
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
    except BaseException:
        await store.release_workspace_lease(
            workspace=str(root),
            owner=owner,
            epoch=lease.epoch,
        )
        raise

    try:
        if len(stdout) > config.max_output_bytes or len(stderr) > config.max_output_bytes:
            raise FDXPolicyError("FDX output exceeded configured bound")
        after = _snapshot_files(jail_root)
        if before != after:
            _restore_files(jail_root, before, expected_current=after)
            raise FDXPolicyError("FDX produced a workspace side effect")
        if process.returncode != 0:
            raise FDXPolicyError(f"FDX exited with status {process.returncode}")
        try:
            response = json.loads(stdout.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FDXPolicyError("FDX returned invalid structured output") from exc
        if (
            not isinstance(response, dict)
            or response.get("protocol") != FDX_PROTOCOL
            or response.get("version") != FDX_VERSION
            or response.get("health") != "ok"
            or response.get("capabilities") != {
                "read_only": True,
                "network_writes": False,
                "workspace_mutation": False,
                "process_persistence": False,
            }
        ):
            raise FDXPolicyError("FDX response protocol mismatch")
        return FDXResult(
            status="succeeded",
            output=response,
            authoritative=False,
            used_fdx=True,
        )
    finally:
        await store.release_workspace_lease(
            workspace=str(root),
            owner=owner,
            epoch=lease.epoch,
        )


async def run_optional_fdx(
    payload: dict[str, Any],
    *,
    workspace: str,
    config: FDXConfig,
    fallback: Callable[[], Awaitable[FDXResult]],
    configured_root: str | None = None,
    store: DurableFlowDeck | None = None,
    run_id: str | None = None,
    owner: str = "flowdeck-fdx",
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
            store=store,
            run_id=run_id,
            owner=owner,
        )
    except (FDXPolicyError, OSError, asyncio.TimeoutError):
        fallback_result = await fallback()
        return FDXResult(
            status=fallback_result.status,
            output=fallback_result.output,
            authoritative=fallback_result.authoritative,
            used_fdx=False,
            fallback_reason="fdx_unavailable_or_failed",
        )