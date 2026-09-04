"""Extended Search API endpoints.

POST /api/search/web    – trigger a web search via the configured backend
POST /api/search/fetch  – fetch and parse a URL
POST /api/search/crawl  – deep-crawl a URL via Firecrawl
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cptr.utils.config import check_access

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/search", tags=["search-extended"])

COOKIE_NAME = "cptr_session"


def _get_user(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    client_host = request.client.host if request.client else "127.0.0.1"
    auth = check_access(client_host=client_host, jwt_token=token)
    if not auth or not auth.user_id:
        raise HTTPException(401, "authentication required")
    return auth.user_id


class WebSearchRequest(BaseModel):
    query: str
    limit: Optional[int] = 10


class FetchRequest(BaseModel):
    url: str
    format: Optional[str] = "text"  # "text" | "markdown" | "html"


class CrawlRequest(BaseModel):
    url: str
    max_pages: Optional[int] = 5
    include_subdomains: Optional[bool] = False


# ── Web search ────────────────────────────────────────────────────────────────


@router.post("/web")
async def web_search(request: Request, body: WebSearchRequest):
    """Trigger a web search via the configured backend and return results."""
    _get_user(request)
    if not body.query.strip():
        raise HTTPException(400, "query is required")
    try:
        from cptr.utils.web.search import web_search_handler

        result_text = await web_search_handler(body.query)
        return {"query": body.query, "result": result_text}
    except Exception as exc:
        raise HTTPException(500, f"Search failed: {exc}")


# ── URL fetch ────────────────────────────────────────────────────────────────


@router.post("/fetch")
async def fetch_url(request: Request, body: FetchRequest):
    """Fetch and parse the content of a URL."""
    _get_user(request)
    if not body.url.strip():
        raise HTTPException(400, "url is required")
    try:
        from cptr.utils.web.reader import read_url_handler

        result = await read_url_handler(body.url)
        return {"url": body.url, "content": result}
    except Exception as exc:
        raise HTTPException(500, f"Fetch failed: {exc}")


# ── Crawl ────────────────────────────────────────────────────────────────────


@router.post("/crawl")
async def crawl_url(request: Request, body: CrawlRequest):
    """Deep-crawl a URL using Firecrawl and return structured markdown."""
    _get_user(request)
    if not body.url.strip():
        raise HTTPException(400, "url is required")
    try:
        import os
        from cptr.models import Config

        api_key = os.environ.get("FIRECRAWL_API_KEY") or str(
            await Config.get("web.firecrawl_api_key") or ""
        )
        if not api_key:
            raise HTTPException(501, "Firecrawl API key is not configured on this server")
        from cptr.utils.web.firecrawl import search as _firecrawl_search

        result = await _firecrawl_search(
            body.url, api_key=api_key, count=min(body.max_pages or 5, 20)
        )
        return {"url": body.url, "result": result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Crawl failed: {exc}")
