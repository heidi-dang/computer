"""Extended chat API endpoints appended to the chat router.

These endpoints are added to the existing /api/chats router:

GET    /api/chats/{chat_id}/messages                   – list all messages (paginated)
GET    /api/chats/{chat_id}/messages/{message_id}       – get single message
DELETE /api/chats/{chat_id}/messages/{message_id}       – delete single message
GET    /api/chats/{chat_id}/export                      – export chat (json/markdown/text)
POST   /api/chats/{chat_id}/title                       – regenerate chat title via LLM
GET    /api/chats/{chat_id}/tokens                      – context token usage
POST   /api/chats/bulk-delete                           – delete multiple chats
GET    /api/chats/{chat_id}/attachments                 – list all file attachments in chat
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from cptr.models import Chat, ChatMessage
from cptr.utils.config import check_access, now_ms

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chats", tags=["chats-extended"])

COOKIE_NAME = "cptr_session"


def _get_user(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    client_host = request.client.host if request.client else "127.0.0.1"
    auth = check_access(client_host=client_host, jwt_token=token)
    if not auth or not auth.user_id:
        raise HTTPException(401, "authentication required")
    return auth.user_id


def _message_dict(m: ChatMessage) -> dict:
    return {
        "id": m.id,
        "chat_id": m.chat_id,
        "parent_id": m.parent_id,
        "role": m.role,
        "content": m.content or "",
        "model": m.model,
        "done": m.done,
        "output": m.output or [],
        "usage": m.usage,
        "meta": m.meta,
        "created_at": m.created_at,
    }


# ── List messages ─────────────────────────────────────────────────────────────


@router.get("/{chat_id}/messages")
async def list_messages(
    request: Request,
    chat_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """List all messages in a chat, ordered by creation time (paginated)."""
    user_id = _get_user(request)
    chat = await Chat.get_by_id(chat_id)
    if not chat or chat.user_id != user_id:
        raise HTTPException(404, "Chat not found")
    messages = await ChatMessage.get_all_by_chat(chat_id)
    page = messages[offset : offset + limit]
    return {
        "chat_id": chat_id,
        "total": len(messages),
        "offset": offset,
        "limit": limit,
        "messages": [_message_dict(m) for m in page],
    }


# ── Get single message ────────────────────────────────────────────────────────


@router.get("/{chat_id}/messages/{message_id}")
async def get_message(request: Request, chat_id: str, message_id: str):
    """Fetch a single message by ID including tool calls and outputs."""
    user_id = _get_user(request)
    chat = await Chat.get_by_id(chat_id)
    if not chat or chat.user_id != user_id:
        raise HTTPException(404, "Chat not found")
    msg = await ChatMessage.get_by_id(message_id)
    if not msg or msg.chat_id != chat_id:
        raise HTTPException(404, "Message not found")
    return _message_dict(msg)


# ── Delete single message ─────────────────────────────────────────────────────


@router.delete("/{chat_id}/messages/{message_id}")
async def delete_message(request: Request, chat_id: str, message_id: str):
    """Delete a single message from a chat's history."""
    user_id = _get_user(request)
    chat = await Chat.get_by_id(chat_id)
    if not chat or chat.user_id != user_id:
        raise HTTPException(404, "Chat not found")
    msg = await ChatMessage.get_by_id(message_id)
    if not msg or msg.chat_id != chat_id:
        raise HTTPException(404, "Message not found")
    deleted = await ChatMessage.delete(message_id)
    return {"ok": deleted, "message_id": message_id}


# ── Export chat ───────────────────────────────────────────────────────────────


@router.get("/{chat_id}/export")
async def export_chat(
    request: Request,
    chat_id: str,
    format: Literal["json", "markdown", "text"] = Query("json"),
):
    """Export an entire chat as JSON, Markdown, or plain text."""
    user_id = _get_user(request)
    chat = await Chat.get_by_id(chat_id)
    if not chat or chat.user_id != user_id:
        raise HTTPException(404, "Chat not found")
    messages = await ChatMessage.get_all_by_chat(chat_id)

    if format == "json":
        msg_map: dict[str, dict] = {}
        for m in messages:
            entry: dict = {
                "id": m.id,
                "parentId": m.parent_id,
                "childrenIds": [],
                "role": m.role,
                "content": m.content or "",
                "timestamp": m.created_at,
            }
            if m.role == "user":
                entry["models"] = [m.model] if m.model else []
            else:
                entry["model"] = m.model
                entry["done"] = m.done
                entry["output"] = m.output or []
                if m.usage:
                    entry["usage"] = m.usage
            if m.chat_summary:
                entry["chat_summary"] = m.chat_summary
            msg_map[m.id] = entry
        for m in messages:
            if m.parent_id and m.parent_id in msg_map:
                msg_map[m.parent_id]["childrenIds"].append(m.id)
        payload = {
            "id": chat.id,
            "title": chat.title,
            "summary": chat.summary,
            "created_at": chat.created_at,
            "updated_at": chat.updated_at,
            "history": {"currentId": chat.current_message_id, "messages": msg_map},
        }
        return payload

    elif format == "markdown":
        lines: list[str] = [f"# {chat.title}", ""]
        for m in messages:
            role_label = "**User**" if m.role == "user" else f"**Assistant** ({m.model or ''})"
            lines.append(f"### {role_label}")
            lines.append(m.content or "")
            lines.append("")
        nl = "\n"
        content_md = nl.join(lines)
        return {"chat_id": chat_id, "format": "markdown", "content": content_md}

    else:  # text
        lines = [f"Chat: {chat.title}", "=" * 60, ""]
        for m in messages:
            role_label = "User" if m.role == "user" else f"Assistant ({m.model or ''})"
            lines.append(f"[{role_label}]")
            lines.append(m.content or "")
            lines.append("")
        return {"chat_id": chat_id, "format": "text", "content": nl.join(lines)}


# ── Regenerate title ──────────────────────────────────────────────────────────


@router.post("/{chat_id}/title")
async def regenerate_title(request: Request, chat_id: str):
    """Regenerate the auto-summary title for a chat using the first user message."""
    user_id = _get_user(request)
    chat = await Chat.get_by_id(chat_id)
    if not chat or chat.user_id != user_id:
        raise HTTPException(404, "Chat not found")
    messages = await ChatMessage.get_all_by_chat(chat_id)
    # Find first user message to derive title from
    first_user = next((m for m in messages if m.role == "user"), None)
    if not first_user:
        raise HTTPException(400, "Chat has no user messages to derive a title from")
    # Build a short title from the first 80 chars of user content
    raw = (first_user.content or "").strip()
    # Derive a short title from the first user message content
    title = (raw[:60] + "…") if len(raw) > 60 else raw or "Untitled"

    updated = await Chat.update_title(chat_id, title, now_ms())
    return {"ok": updated, "chat_id": chat_id, "title": title}


# ── Token usage ───────────────────────────────────────────────────────────────


@router.get("/{chat_id}/tokens")
async def get_token_usage(request: Request, chat_id: str):
    """Get current token count / context usage for a chat."""
    user_id = _get_user(request)
    chat = await Chat.get_by_id(chat_id)
    if not chat or chat.user_id != user_id:
        raise HTTPException(404, "Chat not found")
    messages = await ChatMessage.get_all_by_chat(chat_id)
    total_input = 0
    total_output = 0
    total_chars = 0
    for m in messages:
        if m.usage and isinstance(m.usage, dict):
            total_input += int(m.usage.get("input_tokens", 0) or 0)
            total_output += int(m.usage.get("output_tokens", 0) or 0)
        total_chars += len(m.content or "")
    return {
        "chat_id": chat_id,
        "message_count": len(messages),
        "total_chars": total_chars,
        "cumulative_usage": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output,
        },
    }


# ── Bulk-delete chats ─────────────────────────────────────────────────────────


class BulkDeleteRequest(BaseModel):
    chat_ids: list[str]


@router.post("/bulk-delete")
async def bulk_delete_chats(request: Request, body: BulkDeleteRequest):
    """Delete multiple chats in one call."""
    user_id = _get_user(request)
    if not body.chat_ids:
        return {"ok": True, "deleted": [], "not_found": []}
    chats = await Chat.get_by_ids(body.chat_ids)
    owned = {c.id for c in chats if c.user_id == user_id}
    deleted = []
    not_found = []
    for cid in body.chat_ids:
        if cid in owned:
            ok = await Chat.delete(cid)
            if ok:
                deleted.append(cid)
            else:
                not_found.append(cid)
        else:
            not_found.append(cid)
    return {"ok": True, "deleted": deleted, "not_found": not_found}


# ── List attachments ──────────────────────────────────────────────────────────


@router.get("/{chat_id}/attachments")
async def list_attachments(request: Request, chat_id: str):
    """List all file attachments referenced in a chat."""
    user_id = _get_user(request)
    chat = await Chat.get_by_id(chat_id)
    if not chat or chat.user_id != user_id:
        raise HTTPException(404, "Chat not found")
    messages = await ChatMessage.get_all_by_chat(chat_id)
    attachments: list[dict] = []
    seen: set[str] = set()
    for m in messages:
        meta = m.meta or {}
        files = meta.get("files") or []
        for f in files:
            if isinstance(f, dict):
                fid = f.get("id") or f.get("name") or ""
                if fid and fid not in seen:
                    seen.add(fid)
                    attachments.append(
                        {
                            "message_id": m.id,
                            "role": m.role,
                            **f,
                        }
                    )
    return {"chat_id": chat_id, "attachments": attachments, "count": len(attachments)}
