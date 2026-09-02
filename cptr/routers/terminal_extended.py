"""Extended Terminal API endpoints.

POST /api/terminal/exec                     – one-shot shell command (no pty)
GET  /api/terminal/{session_id}/history     – scrollback buffer for a terminal session
POST /api/terminal/{session_id}/resize      – resize a terminal session (rows × cols)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cptr.utils.config import check_access

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/terminal", tags=["terminal-extended"])

COOKIE_NAME = "cptr_session"


def _get_user(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    client_host = request.client.host if request.client else "127.0.0.1"
    auth = check_access(client_host=client_host, jwt_token=token)
    if not auth or not auth.user_id:
        raise HTTPException(401, "authentication required")
    return auth.user_id


class ExecRequest(BaseModel):
    command: str
    cwd: Optional[str] = None
    timeout: Optional[float] = 30.0  # seconds
    env: Optional[dict[str, str]] = None


class ResizeRequest(BaseModel):
    cols: int
    rows: int


# ── One-shot exec ─────────────────────────────────────────────────────────────


@router.post("/exec")
async def exec_command(request: Request, body: ExecRequest):
    """Run a one-shot shell command and return stdout/stderr/exit-code (no pty)."""
    _get_user(request)
    if not body.command or not body.command.strip():
        raise HTTPException(400, "command is required")
    timeout = min(max(body.timeout or 30.0, 1.0), 300.0)  # cap at 5 min

    import os

    env = {**os.environ, **(body.env or {})}

    try:
        proc = await asyncio.create_subprocess_shell(
            body.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=body.cwd,
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            stdout_bytes, stderr_bytes = await proc.communicate()
            timed_out = True
    except Exception as exc:
        raise HTTPException(500, f"Failed to run command: {exc}")

    return {
        "command": body.command,
        "exit_code": proc.returncode,
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "timed_out": timed_out,
    }


# ── Session history ───────────────────────────────────────────────────────────


@router.get("/{session_id}/history")
async def get_session_history(request: Request, session_id: str):
    """Retrieve the scrollback buffer / output history for a terminal session."""
    _get_user(request)
    from cptr.utils.terminal import manager

    session = manager.get(request, session_id)
    if session is None:
        raise HTTPException(404, f"Terminal session '{session_id}' not found")
    scrollback = session.get_scrollback()
    return {
        "session_id": session_id,
        "history": scrollback.decode("utf-8", errors="replace") if scrollback else "",
        "bytes": len(scrollback) if scrollback else 0,
        "alive": session.is_alive(),
    }


# ── Resize ────────────────────────────────────────────────────────────────────


@router.post("/{session_id}/resize")
async def resize_session(request: Request, session_id: str, body: ResizeRequest):
    """Resize a terminal session (rows × cols)."""
    _get_user(request)
    from cptr.utils.terminal import manager

    session = manager.get(request, session_id)
    if session is None:
        raise HTTPException(404, f"Terminal session '{session_id}' not found")
    if body.cols < 1 or body.rows < 1:
        raise HTTPException(400, "cols and rows must be >= 1")
    try:
        session.resize(body.rows, body.cols)
    except Exception as exc:
        raise HTTPException(500, f"Resize failed: {exc}")
    return {"ok": True, "session_id": session_id, "cols": body.cols, "rows": body.rows}
