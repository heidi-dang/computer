"""Firecrawl search provider.

https://docs.firecrawl.dev/api-reference/endpoint/search
Uses the v2 search endpoint and returns web results formatted for LLM context.
"""

from __future__ import annotations

import httpx

DEFAULT_BASE_URL = "https://api.firecrawl.dev"


async def search(
    query: str,
    api_key: str,
    count: int = 5,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Search using Firecrawl's search API."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/v2/search",
            json={
                "query": query,
                "limit": count,
                "sources": ["web"],
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("success", True):
        error = data.get("error", "Unknown error")
        return f"Firecrawl error: {error}"

    results = []
    for item in data.get("data", {}).get("web", [])[:count]:
        title = item.get("title", "")
        url = item.get("url", "")
        description = item.get("description") or item.get("snippet", "")
        markdown = item.get("markdown", "")

        parts = []
        if title:
            parts.append(f"**{title}**")
        if url:
            parts.append(url)
        if description:
            parts.append(description)
        if markdown:
            if len(markdown) > 1500:
                markdown = markdown[:1500] + "..."
            parts.append(markdown)

        if parts:
            results.append("\n".join(parts))

    warning = data.get("warning")
    if warning and results:
        results.append(f"Warning: {warning}")

    return "\n\n".join(results) if results else "No results found."
