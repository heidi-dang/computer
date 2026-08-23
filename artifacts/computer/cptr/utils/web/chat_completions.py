"""Chat Completions search provider: any OpenAI-compatible endpoint.

Send the search query as a user message to a /chat/completions endpoint
and return the model's response. Works with Perplexity Sonar (web-grounded),
LiteLLM proxy, or any model that returns search-grounded results.
"""

from __future__ import annotations

import httpx

_SYSTEM_PROMPT = (
    "You are a web search assistant. Given the user's query, provide a concise, "
    "factual answer with relevant sources. Format each result as:\n"
    "**Title**\nURL\nSnippet\n\n"
    "Include up to 5 results. Cite your sources with URLs when possible."
)


async def search(
    query: str,
    api_key: str,
    base_url: str,
    model: str,
) -> str:
    """Search using an OpenAI-compatible chat completions endpoint."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": query},
                ],
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return content or "No results found."
