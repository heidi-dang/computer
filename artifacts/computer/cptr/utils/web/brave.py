"""Brave Search provider.

https://brave.com/search/api/. Free tier: 2000 queries/month.
"""

from __future__ import annotations

import httpx


async def search(query: str, api_key: str, count: int = 5) -> str:
    """Search using Brave Search API."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": count},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("web", {}).get("results", [])[:count]:
        title = item.get("title", "")
        url = item.get("url", "")
        desc = item.get("description", "")
        results.append(f"**{title}**\n{url}\n{desc}")

    return "\n\n".join(results) if results else "No results found."
