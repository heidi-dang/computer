"""Perplexity Search provider: raw web search results.

https://docs.perplexity.ai – dedicated search endpoint returning
structured results (title, URL, snippet) without LLM synthesis.
"""

from __future__ import annotations

import httpx


async def search(
    query: str,
    api_key: str,
    count: int = 5,
    base_url: str = "https://api.perplexity.ai",
) -> str:
    """Search using Perplexity's dedicated search API."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/search",
            json={"query": query},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", [])[:count]:
        title = item.get("title", "")
        url = item.get("url", "")
        snippet = item.get("snippet", "")
        results.append(f"**{title}**\n{url}\n{snippet}")

    return "\n\n".join(results) if results else "No results found."
