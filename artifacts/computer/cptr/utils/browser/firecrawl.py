"""Firecrawl browser provider — page-to-markdown via REST API.

Pure httpx, no SDK dependency. Supports both cloud (api.firecrawl.dev) and
self-hosted instances.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.firecrawl.dev"


async def scrape(
    url: str,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    format: str = "markdown",
) -> str:
    """Scrape a single page and return content as markdown.

    Uses POST /v1/scrape.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"url": url, "formats": [format]}

    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            f"{base_url.rstrip('/')}/v1/scrape",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("success"):
        error = data.get("error", "Unknown error")
        return f"Firecrawl error: {error}"

    result = data.get("data", {})
    content = result.get(format, result.get("markdown", ""))

    if not content:
        return f"Firecrawl returned empty content for {url}"

    # Trim to reasonable size for LLM context
    if len(content) > 50_000:
        content = content[:50_000] + "\n\n[... truncated]"

    return content
