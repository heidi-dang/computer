"""DuckDuckGo search: zero-config fallback (no API key needed).

Scrapes the HTML lite endpoint. No rate-limit guarantees, but
works without any setup, making it a useful default.
"""

from __future__ import annotations

import re

import httpx


async def search(query: str, count: int = 5) -> str:
    """Search using DuckDuckGo HTML lite (no API key)."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (compatible; cptr/1.0)"},
            )
            resp.raise_for_status()

        html = resp.text
        results = []

        # Try structured result extraction
        result_pattern = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL,
        )

        for match in result_pattern.finditer(html):
            url = match.group(1)
            title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            snippet = re.sub(r"<[^>]+>", "", match.group(3)).strip()
            if title and url:
                results.append(f"**{title}**\n{url}\n{snippet}")
                if len(results) >= count:
                    break

        # Simpler fallback
        if not results:
            link_pattern = re.compile(
                r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                re.DOTALL,
            )
            for match in link_pattern.finditer(html):
                url = match.group(1)
                title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
                if title and url and not url.startswith("/"):
                    results.append(f"**{title}**\n{url}")
                    if len(results) >= count:
                        break

        return "\n\n".join(results) if results else "No results found."

    except Exception as e:
        return f"Error searching DuckDuckGo: {e}"
