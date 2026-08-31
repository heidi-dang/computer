"""Extended Skills API: CRUD + test endpoints.

GET    /api/skills/{skill_name}             – get a single skill's full content
POST   /api/skills                          – create a new skill
PUT    /api/skills/{skill_name}             – update an existing skill
DELETE /api/skills/{skill_name}             – delete a managed skill
POST   /api/skills/{skill_name}/test        – test skill against a sample prompt
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from cptr.utils.config import check_access
from cptr.utils.skills import (
    create_managed_skill,
    delete_managed_skill,
    discover_skills,
    update_managed_skill,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["skills-extended"])

COOKIE_NAME = "cptr_session"


def _get_user(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    client_host = request.client.host if request.client else "127.0.0.1"
    auth = check_access(client_host=client_host, jwt_token=token)
    if not auth or not auth.user_id:
        raise HTTPException(401, "authentication required")
    return auth.user_id


class CreateSkillRequest(BaseModel):
    name: str
    content: str  # Full SKILL.md content including YAML frontmatter
    workspace: str = ""
    scope: Literal["workspace", "global"] = "workspace"
    created_from: Optional[str] = None


class UpdateSkillRequest(BaseModel):
    content: str  # Updated SKILL.md content
    workspace: str = ""


class TestSkillRequest(BaseModel):
    prompt: str
    workspace: str = ""
    model_id: Optional[str] = None


# ── Get single skill ──────────────────────────────────────────────────────────


@router.get("/{skill_name}")
async def get_skill(
    request: Request,
    skill_name: str,
    workspace: str = Query("", description="Workspace path; omit for global skills"),
):
    """Get a single skill's full content and metadata."""
    _get_user(request)
    from cptr.utils.skills import load_skill

    skills = await asyncio.to_thread(discover_skills, workspace)
    meta = next((s for s in skills if s.name == skill_name), None)
    if meta is None:
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    try:
        content = await asyncio.to_thread(load_skill, workspace, skill_name)
    except Exception as exc:
        raise HTTPException(500, f"Failed to load skill: {exc}")
    if content is None:
        raise HTTPException(404, f"Skill '{skill_name}' could not be loaded")
    return {
        "name": content.name,
        "description": content.description,
        "location": content.location,
        "source": content.source,
        "license": content.license,
        "compatibility": content.compatibility,
        "managed": content.managed,
        "created_by": content.created_by,
        "created_from": content.created_from,
        "content": content.content,
        "body": content.body,
        "resources": content.resources,
    }


# ── Create skill ──────────────────────────────────────────────────────────────


@router.post("")
async def create_skill(request: Request, body: CreateSkillRequest):
    """Create / register a new managed skill."""
    _get_user(request)
    try:
        result = await asyncio.to_thread(
            create_managed_skill,
            body.workspace,
            body.name,
            body.content,
            body.scope,
            body.created_from,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Failed to create skill: {exc}")
    return result


# ── Update skill ──────────────────────────────────────────────────────────────


@router.put("/{skill_name}")
async def update_skill(request: Request, skill_name: str, body: UpdateSkillRequest):
    """Update an existing managed skill's SKILL.md content."""
    _get_user(request)
    try:
        result = await asyncio.to_thread(
            update_managed_skill,
            body.workspace,
            skill_name,
            body.content,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Failed to update skill: {exc}")
    return result


# ── Delete skill ──────────────────────────────────────────────────────────────


@router.delete("/{skill_name}")
async def delete_skill(
    request: Request,
    skill_name: str,
    workspace: str = Query("", description="Workspace path; omit for global skills"),
):
    """Delete a managed skill (removes its directory)."""
    _get_user(request)
    try:
        result = await asyncio.to_thread(delete_managed_skill, workspace, skill_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Failed to delete skill: {exc}")
    return result


# ── Test skill ────────────────────────────────────────────────────────────────


@router.post("/{skill_name}/test")
async def test_skill(request: Request, skill_name: str, body: TestSkillRequest):
    """Test a skill by loading its content and returning how it would augment a prompt."""
    _get_user(request)
    from cptr.utils.skills import load_skill, format_skill_content

    try:
        content = await asyncio.to_thread(load_skill, body.workspace, skill_name)
    except Exception as exc:
        raise HTTPException(500, f"Failed to load skill: {exc}")
    if content is None:
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    try:
        formatted = format_skill_content(content)
    except Exception as exc:
        raise HTTPException(500, f"Failed to format skill: {exc}")

    return {
        "skill_name": skill_name,
        "prompt": body.prompt,
        "augmented_system_context": formatted,
        "instruction_char_count": len(formatted),
    }
