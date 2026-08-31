"""Extended Browser API endpoints.

POST /api/browser/sessions/{session_id}/screenshot – take a screenshot
POST /api/browser/sessions/{session_id}/navigate   – navigate to URL
POST /api/browser/sessions/{session_id}/click      – click at coordinate or selector
POST /api/browser/sessions/{session_id}/type       – type text into focused element
GET  /api/browser/sessions/{session_id}/cookies    – get all cookies
DELETE /api/browser/sessions/{session_id}/cookies  – clear cookies
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from cptr.utils.config import AuthResult, check_access

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/browser", tags=["browser-extended"])

COOKIE_NAME = "cptr_session"


def _auth(request: Request) -> AuthResult:
    token = request.cookies.get(COOKIE_NAME)
    client_host = request.client.host if request.client else "127.0.0.1"
    auth = check_access(client_host=client_host, jwt_token=token)
    if not auth or not auth.user_id:
        raise HTTPException(401, "authentication required")
    return auth


def _owner(auth: AuthResult) -> str:
    return auth.user_id or auth.username or "default"


async def _get_cdp_client(session_id: str, owner: str):
    """Get a CDPClient for the named browser session."""
    from cptr.utils.browser.proxy import manager
    from cptr.utils.browser.viewer import manager as chrome_manager, resolve_cdp_endpoint

    # Try the chrome_viewer_manager first (for personal-chrome sessions)
    try:
        cdp_url = await resolve_cdp_endpoint()
        if cdp_url:
            from cptr.utils.browser.cdp import CDPClient
            client = await CDPClient.connect(cdp_url)
            return client
    except Exception:
        pass

    # Fallback: raise informative error
    raise HTTPException(
        503,
        "No CDP-capable browser session found. "
        "Enable Personal Chrome with CDP URL configured, or use a chrome-mode browser session.",
    )


class NavigateRequest(BaseModel):
    url: str
    wait_for_load: Optional[bool] = True


class ClickRequest(BaseModel):
    ref: str  # accessibility ref (e.g. "@e42") or CSS selector


class TypeRequest(BaseModel):
    ref: str  # accessibility ref or CSS selector
    text: str


class ScreenshotRequest(BaseModel):
    width: Optional[int] = None
    height: Optional[int] = None
    format: Optional[str] = "png"  # "png" | "base64"


# ── Screenshot ────────────────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/screenshot")
async def take_screenshot(request: Request, session_id: str, body: ScreenshotRequest):
    """Take a screenshot of the current browser session."""
    auth = _auth(request)
    try:
        cdp = await _get_cdp_client(session_id, _owner(auth))
        try:
            png_bytes = await cdp.screenshot(width=body.width, height=body.height)
        finally:
            await cdp.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Screenshot failed: {exc}")

    if body.format == "base64":
        return {
            "session_id": session_id,
            "format": "base64",
            "data": base64.b64encode(png_bytes).decode(),
            "size_bytes": len(png_bytes),
        }
    return Response(content=png_bytes, media_type="image/png")


# ── Navigate ──────────────────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/navigate")
async def navigate_browser(request: Request, session_id: str, body: NavigateRequest):
    """Navigate the browser session to a URL."""
    auth = _auth(request)
    if not body.url.strip():
        raise HTTPException(400, "url is required")
    try:
        cdp = await _get_cdp_client(session_id, _owner(auth))
        try:
            result = await cdp.navigate(body.url)
        finally:
            await cdp.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Navigation failed: {exc}")
    return {"session_id": session_id, "url": body.url, "result": result}


# ── Click ─────────────────────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/click")
async def click_element(request: Request, session_id: str, body: ClickRequest):
    """Click at an accessibility ref or coordinate within the browser session."""
    auth = _auth(request)
    if not body.ref.strip():
        raise HTTPException(400, "ref is required")
    try:
        cdp = await _get_cdp_client(session_id, _owner(auth))
        try:
            await cdp.click(body.ref)
        finally:
            await cdp.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Click failed: {exc}")
    return {"session_id": session_id, "ref": body.ref, "ok": True}


# ── Type ──────────────────────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/type")
async def type_text(request: Request, session_id: str, body: TypeRequest):
    """Type text into the focused element of a browser session."""
    auth = _auth(request)
    if not body.ref.strip():
        raise HTTPException(400, "ref is required")
    try:
        cdp = await _get_cdp_client(session_id, _owner(auth))
        try:
            await cdp.type_text(body.ref, body.text)
        finally:
            await cdp.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Type failed: {exc}")
    return {"session_id": session_id, "ref": body.ref, "text": body.text, "ok": True}


# ── Get cookies ───────────────────────────────────────────────────────────────


@router.get("/sessions/{session_id}/cookies")
async def get_cookies(request: Request, session_id: str):
    """Get all cookies for the current browser session."""
    auth = _auth(request)
    try:
        cdp = await _get_cdp_client(session_id, _owner(auth))
        try:
            result = await cdp._send("Network.getAllCookies")
            cookies = result.get("cookies", [])
        finally:
            await cdp.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to get cookies: {exc}")
    return {"session_id": session_id, "cookies": cookies, "count": len(cookies)}


# ── Delete cookies ────────────────────────────────────────────────────────────


@router.delete("/sessions/{session_id}/cookies")
async def clear_cookies(request: Request, session_id: str):
    """Clear all cookies for the current browser session."""
    auth = _auth(request)
    try:
        cdp = await _get_cdp_client(session_id, _owner(auth))
        try:
            await cdp._send("Network.clearBrowserCookies")
        finally:
            await cdp.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to clear cookies: {exc}")
    return {"session_id": session_id, "ok": True}
