"""Extended Automations API endpoints.

GET  /api/automations/{automation_id}/runs/{run_id}  – get run detail
POST /api/automations/bulk-toggle                     – enable/disable multiple automations
GET  /api/automations/upcoming                        – list next scheduled triggers
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from cptr.models.automations import Automation, AutomationRun
from cptr.utils.automations import next_n_runs_ns
from cptr.utils.config import check_access, now_ms

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/automations", tags=["automations-extended"])

COOKIE_NAME = "cptr_session"


def _get_user(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    client_host = request.client.host if request.client else "127.0.0.1"
    auth = check_access(client_host=client_host, jwt_token=token)
    if not auth or not auth.user_id:
        raise HTTPException(401, "authentication required")
    return auth.user_id


def _run_dict(r: AutomationRun) -> dict:
    return {
        "id": r.id,
        "automation_id": r.automation_id,
        "chat_id": r.chat_id,
        "status": r.status,
        "error": r.error,
        "created_at": r.created_at,
    }


# ── Get run detail ────────────────────────────────────────────────────────────


@router.get("/{automation_id}/runs/{run_id}")
async def get_run_detail(request: Request, automation_id: str, run_id: str):
    """Get the full output/log for a specific automation run."""
    user_id = _get_user(request)
    automation = await Automation.get_by_id(automation_id)
    if not automation or automation.user_id != user_id:
        raise HTTPException(404, "Automation not found")
    # Look up the run from the automation's run list
    runs = await AutomationRun.get_by_automation(automation_id, limit=200)
    run = next((r for r in runs if r.id == run_id), None)
    if not run:
        raise HTTPException(404, "Run not found")

    # Try to fetch the associated chat output if available
    chat_output = None
    if run.chat_id:
        try:
            from cptr.models import ChatMessage

            messages = await ChatMessage.get_all_by_chat(run.chat_id)
            chat_output = [
                {
                    "role": m.role,
                    "content": m.content or "",
                    "model": m.model,
                    "created_at": m.created_at,
                }
                for m in messages
            ]
        except Exception:
            pass

    return {
        **_run_dict(run),
        "chat_output": chat_output,
    }


# ── Bulk toggle ───────────────────────────────────────────────────────────────


class BulkToggleRequest(BaseModel):
    automation_ids: list[str]
    active: bool


@router.post("/bulk-toggle")
async def bulk_toggle_automations(request: Request, body: BulkToggleRequest):
    """Enable or disable multiple automations at once."""
    user_id = _get_user(request)
    if not body.automation_ids:
        return {"ok": True, "toggled": [], "not_found": []}

    toggled = []
    not_found = []
    for aid in body.automation_ids:
        automation = await Automation.get_by_id(aid)
        if not automation or automation.user_id != user_id:
            not_found.append(aid)
            continue
        try:
            await Automation.update_by_id(
                aid,
                is_active=body.active,
                updated_at=now_ms(),
            )
            toggled.append(aid)
        except Exception as exc:
            log.warning("Failed to toggle automation %s: %s", aid, exc)
            not_found.append(aid)

    return {"ok": True, "active": body.active, "toggled": toggled, "not_found": not_found}


# ── Upcoming scheduled runs ───────────────────────────────────────────────────


@router.get("/upcoming")
async def list_upcoming_runs(
    request: Request,
    workspace: Optional[str] = Query(None),
    limit: int = Query(5, ge=1, le=20, description="How many upcoming runs per automation"),
):
    """List the next scheduled trigger times for all active automations."""
    user_id = _get_user(request)
    items, _ = await Automation.get_by_workspace(
        user_id=user_id,
        workspace=workspace,
        status="active",
        limit=200,
    )
    result = []
    for a in items:
        if not a.is_active or not a.rrule:
            continue
        try:
            next_runs = next_n_runs_ns(a.rrule, n=limit)
        except Exception:
            next_runs = []
        result.append(
            {
                "id": a.id,
                "name": a.name,
                "workspace": a.workspace,
                "rrule": a.rrule,
                "next_runs": next_runs,
            }
        )
    # Sort by nearest upcoming run
    result.sort(key=lambda x: x["next_runs"][0] if x["next_runs"] else float("inf"))
    return {"automations": result, "count": len(result)}
