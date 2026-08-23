"""Tavily search provider: purpose-built for AI agents.

https://tavily.com. Provides AI-optimized search with optional
answer synthesis across results.
"""

from __future__ import annotations

import httpx


async def search(query: str, api_key: str, count: int = 5) -> str:
    """Search using Tavily's AI-optimized search API."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": count,
                "include_answer": True,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    parts = []

    answer = data.get("answer")
    if answer:
        parts.append(f"**Summary:** {answer}")

    for item in data.get("results", [])[:count]:
        title = item.get("title", "")
        url = item.get("url", "")
        content = item.get("content", "")
        parts.append(f"**{title}**\n{url}\n{content}")

    return "\n\n".join(parts) if parts else "No results found."
