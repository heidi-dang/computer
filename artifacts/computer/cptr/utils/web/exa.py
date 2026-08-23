"""Exa search provider: neural search purpose-built for AI.

https://exa.ai
Uses the /search + /contents endpoints for
high-quality results with full page content extraction.
"""

from __future__ import annotations

import httpx


async def search(query: str, api_key: str, count: int = 5) -> str:
    """Search using Exa's neural search API.

    Exa returns semantically relevant results with optional full-text
    content extraction, ideal for AI tool use since the model gets
    actual page content, not just snippets.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.exa.ai/search",
            json={
                "query": query,
                "numResults": count,
                "contents": {
                    "text": {"maxCharacters": 2000},
                },
                "type": "auto",
            },
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", [])[:count]:
        title = item.get("title", "")
        url = item.get("url", "")
        text = item.get("text", "")

        # Trim content to a reasonable length per result
        if len(text) > 1500:
            text = text[:1500] + "…"

        parts = []
        if title:
            parts.append(f"**{title}**")
        if url:
            parts.append(url)
        if text:
            parts.append(text)

        results.append("\n".join(parts))

    return "\n\n---\n\n".join(results) if results else "No results found."
